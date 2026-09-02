"""Transactional notification outbox and channel-specific provider registry.

The deterministic adapters are observable local fixtures: tests consume the
same envelope that a live SMTP or Twilio transport would receive.  Production
configuration rejects those adapters, preventing a no-network submission from
being mistaken for real delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import smtplib
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.core.config import settings
from app.core.db import set_rls_actor, set_rls_clinic
from app.core.field_crypto import field_codec
from app.models import (
    ClinicMembership,
    ClinicOperationalSetting,
    NotificationAttempt,
    NotificationOutbox,
    NotificationReceipt,
    User,
    get_datetime_utc,
)

_NOTIFICATION_RETRY_DELAYS_SECONDS = (30, 120, 600, 1_800, 3_600)


def bind_notification_worker(session: Session, clinic_id: uuid.UUID) -> bool:
    """Bind a clinic service identity after a transaction boundary."""

    if session.get_bind().dialect.name == "postgresql":
        pg_row = (
            session.connection()
            .execute(
                text("SELECT * FROM app_lookup_clinic_worker(:clinic_id)"),
                {"clinic_id": clinic_id},
            )
            .one_or_none()
        )
        if pg_row is None:
            return False
        user_id = uuid.UUID(str(pg_row.user_id))
    else:
        sqlite_row = session.exec(
            select(ClinicMembership, User)
            .join(User)
            .where(
                ClinicMembership.clinic_id == clinic_id,
                ClinicMembership.role == "worker",
                col(ClinicMembership.is_active).is_(True),
                col(User.is_active).is_(True),
                User.account_kind == "service",
            )
            .order_by(col(ClinicMembership.created_at), col(ClinicMembership.id))
        ).first()
        if sqlite_row is None:
            return False
        _membership, user = sqlite_row
        user_id = user.id
    set_rls_clinic(session, clinic_id)
    set_rls_actor(session, user_id, role="worker")
    return True


@dataclass(frozen=True)
class DeliverySubmission:
    provider: str
    message_id: str
    state: str = "submitted"


class NotificationProvider(Protocol):
    name: str

    def send(
        self,
        *,
        channel: str,
        destination: str,
        template_key: str,
        payload: dict[str, object],
        idempotency_key: str,
        callback_url: str | None = None,
    ) -> DeliverySubmission: ...


@dataclass(frozen=True)
class NotificationChannelCapability:
    channel: str
    provider: str
    configured: bool
    production_safe: bool
    reason_code: str | None = None


@dataclass(frozen=True)
class DeterministicInboxMessage:
    provider: str
    message_id: str
    channel: str
    destination: str
    template_key: str
    payload: dict[str, object]
    idempotency_key: str
    submitted_at: datetime


_DETERMINISTIC_INBOX_LOCK = threading.Lock()
_DETERMINISTIC_INBOX: list[DeterministicInboxMessage] = []
_DETERMINISTIC_SUBMISSIONS: dict[tuple[str, str], DeliverySubmission] = {}


def clear_deterministic_inbox() -> None:
    """Reset the process-local fake mailbox used only by deterministic tests."""

    with _DETERMINISTIC_INBOX_LOCK:
        _DETERMINISTIC_INBOX.clear()
        _DETERMINISTIC_SUBMISSIONS.clear()


def deterministic_inbox_messages(
    *, channel: str | None = None, destination: str | None = None
) -> list[DeterministicInboxMessage]:
    with _DETERMINISTIC_INBOX_LOCK:
        return [
            item
            for item in _DETERMINISTIC_INBOX
            if (channel is None or item.channel == channel)
            and (destination is None or item.destination == destination)
        ]


class DeterministicNotificationProvider:
    """Observable no-network adapter whose lifecycle uses signed fake receipts."""

    name = "deterministic"

    def __init__(self, *, expected_channel: str | None = None) -> None:
        self.expected_channel = expected_channel

    def send(
        self,
        *,
        channel: str,
        destination: str,
        template_key: str,
        payload: dict[str, object],
        idempotency_key: str,
        callback_url: str | None = None,
    ) -> DeliverySubmission:
        del callback_url
        if self.expected_channel is not None and channel != self.expected_channel:
            raise ValueError("NOTIFICATION_CHANNEL_PROVIDER_MISMATCH")
        provider_name = (
            f"deterministic-{self.expected_channel}"
            if self.expected_channel is not None
            else self.name
        )
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        key = (provider_name, idempotency_key)
        with _DETERMINISTIC_INBOX_LOCK:
            existing = _DETERMINISTIC_SUBMISSIONS.get(key)
            if existing is not None:
                return existing
            submission = DeliverySubmission(
                provider=provider_name, message_id=f"local-{digest}"
            )
            _DETERMINISTIC_SUBMISSIONS[key] = submission
            _DETERMINISTIC_INBOX.append(
                DeterministicInboxMessage(
                    provider=provider_name,
                    message_id=submission.message_id,
                    channel=channel,
                    destination=destination,
                    template_key=template_key,
                    payload=dict(payload),
                    idempotency_key=idempotency_key,
                    submitted_at=get_datetime_utc(),
                )
            )
            return submission


class DeterministicEmailProvider(DeterministicNotificationProvider):
    def __init__(self) -> None:
        super().__init__(expected_channel="email")


class DeterministicSMSProvider(DeterministicNotificationProvider):
    def __init__(self) -> None:
        super().__init__(expected_channel="sms")


class DeterministicWhatsAppProvider(DeterministicNotificationProvider):
    def __init__(self) -> None:
        super().__init__(expected_channel="whatsapp")


def _render_message(template_key: str, payload: dict[str, object]) -> tuple[str, str]:
    subject = {
        "patient-enrollment-v1": "Your Nightingale patient portal invitation",
        "patient-otp-v1": "Your Nightingale one-time code",
        "staff-invitation-v1": "Your Nightingale clinic invitation",
        "appointment-v1": "Nightingale appointment update",
        "correction-v1": "Important correction to shared care information",
    }.get(template_key, "Nightingale notification")
    lines = [subject]
    for key, value in sorted(payload.items()):
        if value is not None:
            lines.append(f"{key.replace('_', ' ').title()}: {value}")
    return subject, "\n\n".join(lines)


class SMTPNotificationProvider:
    name = "smtp"

    def send(
        self,
        *,
        channel: str,
        destination: str,
        template_key: str,
        payload: dict[str, object],
        idempotency_key: str,
        callback_url: str | None = None,
    ) -> DeliverySubmission:
        del callback_url
        if channel != "email" or not settings.emails_enabled or not settings.SMTP_HOST:
            raise RuntimeError("SMTP_NOTIFICATION_UNAVAILABLE")
        subject, body = _render_message(template_key, payload)
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(
            (
                settings.EMAILS_FROM_NAME or settings.PROJECT_NAME,
                str(settings.EMAILS_FROM_EMAIL),
            )
        )
        message["To"] = destination
        message_id = make_msgid(
            idstring=hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        )
        message["Message-ID"] = message_id
        message["X-Nightingale-Idempotency-Key"] = hashlib.sha256(
            idempotency_key.encode()
        ).hexdigest()
        message.set_content(body)
        smtp_type: type[smtplib.SMTP] = (
            smtplib.SMTP_SSL if settings.SMTP_SSL else smtplib.SMTP
        )
        with smtp_type(
            settings.SMTP_HOST,
            settings.SMTP_PORT,
            timeout=settings.NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
        ) as smtp:
            if settings.SMTP_TLS and not settings.SMTP_SSL:
                smtp.starttls()
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(message)
        return DeliverySubmission(provider=self.name, message_id=message_id)


class TwilioNotificationProvider:
    def __init__(self, *, channel: str) -> None:
        if channel not in {"sms", "whatsapp"}:
            raise ValueError("TWILIO_CHANNEL_INVALID")
        self.channel = channel
        self.name = f"twilio-{channel}"

    def send(
        self,
        *,
        channel: str,
        destination: str,
        template_key: str,
        payload: dict[str, object],
        idempotency_key: str,
        callback_url: str | None = None,
    ) -> DeliverySubmission:
        if channel != self.channel:
            raise ValueError("NOTIFICATION_CHANNEL_PROVIDER_MISMATCH")
        if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
            raise RuntimeError("TWILIO_NOTIFICATION_UNAVAILABLE")
        from_number = (
            settings.TWILIO_SMS_FROM
            if channel == "sms"
            else settings.TWILIO_WHATSAPP_FROM
        )
        if not from_number:
            raise RuntimeError("TWILIO_NOTIFICATION_UNAVAILABLE")
        if not callback_url:
            raise RuntimeError("TWILIO_CALLBACK_URL_UNAVAILABLE")
        _subject, body = _render_message(template_key, payload)
        prefix = "whatsapp:" if channel == "whatsapp" else ""
        url = (
            f"{settings.TWILIO_API_BASE_URL.rstrip('/')}/2010-04-01/Accounts/"
            f"{settings.TWILIO_ACCOUNT_SID}/Messages.json"
        )
        timeout = httpx.Timeout(
            settings.NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
            connect=settings.NOTIFICATION_CONNECT_TIMEOUT_SECONDS,
        )
        response = httpx.post(
            url,
            data={
                "To": f"{prefix}{destination}",
                "From": (
                    from_number
                    if from_number.startswith(prefix)
                    else f"{prefix}{from_number}"
                ),
                "Body": body,
                "StatusCallback": callback_url,
            },
            headers={"Idempotency-Key": idempotency_key},
            auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            timeout=timeout,
        )
        response.raise_for_status()
        body_json = response.json()
        message_id = body_json.get("sid")
        if not isinstance(message_id, str) or not message_id:
            raise RuntimeError("TWILIO_RESPONSE_INVALID")
        return DeliverySubmission(provider=self.name, message_id=message_id)


class NotificationIdempotencyConflict(ValueError):
    """The same idempotency key was reused for a different delivery intent."""


class NotificationChannelUnavailable(ValueError):
    """The clinic or deployment has not enabled the requested channel."""


def _configured_provider_name(channel: str) -> str:
    if channel == "email":
        return settings.NOTIFICATION_EMAIL_PROVIDER
    if channel == "sms":
        return settings.NOTIFICATION_SMS_PROVIDER
    if channel == "whatsapp":
        return settings.NOTIFICATION_WHATSAPP_PROVIDER
    if channel == "portal":
        return "deterministic" if settings.FASTAPI_ENV == "development" else "disabled"
    return "disabled"


def notification_channel_capabilities() -> dict[str, NotificationChannelCapability]:
    capabilities: dict[str, NotificationChannelCapability] = {}
    for channel in ("email", "sms", "whatsapp"):
        provider = _configured_provider_name(channel)
        configured = provider != "disabled"
        reason_code: str | None = None
        if provider == "smtp" and not settings.emails_enabled:
            configured = False
            reason_code = "smtp_not_configured"
        elif provider == "twilio":
            configured = bool(
                settings.TWILIO_ACCOUNT_SID
                and settings.TWILIO_AUTH_TOKEN
                and settings.NOTIFICATION_PUBLIC_BASE_URL
            )
            if channel == "sms":
                configured = configured and bool(settings.TWILIO_SMS_FROM)
            else:
                configured = configured and bool(settings.TWILIO_WHATSAPP_FROM)
            if not configured:
                reason_code = "twilio_not_configured"
        elif provider == "disabled":
            reason_code = "channel_disabled"
        capabilities[channel] = NotificationChannelCapability(
            channel=channel,
            provider=provider,
            configured=configured,
            production_safe=provider not in {"deterministic", "disabled"},
            reason_code=reason_code,
        )
    return capabilities


def _provider(channel: str) -> NotificationProvider:
    configured = _configured_provider_name(channel)
    if configured == "deterministic":
        if channel == "email":
            return DeterministicEmailProvider()
        if channel == "sms":
            return DeterministicSMSProvider()
        if channel == "whatsapp":
            return DeterministicWhatsAppProvider()
        return DeterministicNotificationProvider(expected_channel=channel)
    if configured == "smtp" and channel == "email":
        return SMTPNotificationProvider()
    if configured == "twilio" and channel in {"sms", "whatsapp"}:
        return TwilioNotificationProvider(channel=channel)
    raise RuntimeError("NOTIFICATION_PROVIDER_UNAVAILABLE")


def twilio_status_callback_url(
    *, clinic_id: uuid.UUID, notification_id: uuid.UUID
) -> str | None:
    if settings.NOTIFICATION_PUBLIC_BASE_URL is None:
        return None
    base = str(settings.NOTIFICATION_PUBLIC_BASE_URL).rstrip("/")
    return (
        f"{base}{settings.API_V1_STR}/notification-webhooks/twilio/"
        f"{clinic_id}/{notification_id}"
    )


def normalize_destination(destination: str, channel: str) -> str:
    value = destination.strip()
    if channel == "email":
        _name, parsed = parseaddr(value)
        if parsed != value or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", parsed):
            raise ValueError("NOTIFICATION_EMAIL_INVALID")
        return parsed.lower()
    if channel in {"sms", "whatsapp"}:
        candidate = value.replace(" ", "").replace("-", "")
        if candidate.startswith("00"):
            candidate = f"+{candidate[2:]}"
        if not re.fullmatch(r"\+[1-9]\d{7,14}", candidate):
            raise ValueError("NOTIFICATION_PHONE_INVALID")
        return candidate
    if channel == "portal" and value:
        return value
    raise ValueError("NOTIFICATION_DESTINATION_INVALID")


def _mask_destination(destination: str, channel: str) -> str:
    value = destination.strip()
    if channel == "email" and "@" in value:
        local, domain = value.rsplit("@", 1)
        return f"{local[:1]}***@{domain}"
    digits = "".join(character for character in value if character.isdigit())
    return f"***{digits[-4:]}" if digits else "***"


def _same_notification_intent(
    existing: NotificationOutbox,
    *,
    clinic_id: uuid.UUID,
    purpose: str,
    channel: str,
    destination: str,
    template_key: str,
    payload: dict[str, object],
    patient_id: uuid.UUID | None,
    visit_id: uuid.UUID | None,
    publication_id: uuid.UUID | None,
    portal_invitation_id: uuid.UUID | None,
) -> bool:
    if (
        existing.clinic_id != clinic_id
        or existing.purpose != purpose
        or existing.channel != channel
        or existing.template_key != template_key
        or existing.patient_id != patient_id
        or existing.visit_id != visit_id
        or existing.publication_id != publication_id
        or existing.portal_invitation_id != portal_invitation_id
    ):
        return False
    stored_destination = field_codec.decrypt_text(
        clinic_id,
        "notification.destination",
        existing.id,
        existing.destination_ciphertext,
    )
    stored_payload = field_codec.decrypt_json(
        clinic_id,
        "notification.payload",
        existing.id,
        existing.payload_ciphertext,
    )
    return stored_destination == destination.strip() and stored_payload == payload


def validate_notification_destination(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    channel: str,
    destination: str,
) -> str:
    """Validate clinic policy, deployment capability, and destination syntax."""

    try:
        normalized_destination = normalize_destination(destination, channel)
    except ValueError as exc:
        raise NotificationChannelUnavailable(str(exc)) from exc
    operational = session.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == clinic_id
        )
    ).first()
    if operational is not None and channel != "portal":
        if channel not in operational.messaging_channels_json:
            raise NotificationChannelUnavailable("CLINIC_NOTIFICATION_CHANNEL_DISABLED")
    capability = notification_channel_capabilities().get(channel)
    if channel != "portal" and (capability is None or not capability.configured):
        raise NotificationChannelUnavailable("NOTIFICATION_CHANNEL_UNAVAILABLE")
    return normalized_destination


def queue_notification(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    purpose: str,
    channel: str,
    destination: str,
    template_key: str,
    payload: dict[str, object],
    idempotency_key: str,
    patient_id: uuid.UUID | None = None,
    visit_id: uuid.UUID | None = None,
    publication_id: uuid.UUID | None = None,
    portal_invitation_id: uuid.UUID | None = None,
    created_by_membership_id: uuid.UUID | None = None,
) -> tuple[NotificationOutbox, bool]:
    normalized_destination = validate_notification_destination(
        session,
        clinic_id=clinic_id,
        channel=channel,
        destination=destination,
    )
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    existing = session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.clinic_id == clinic_id,
            NotificationOutbox.idempotency_key == digest,
        )
    ).first()
    if existing is not None:
        if not _same_notification_intent(
            existing,
            clinic_id=clinic_id,
            purpose=purpose,
            channel=channel,
            destination=normalized_destination,
            template_key=template_key,
            payload=payload,
            patient_id=patient_id,
            visit_id=visit_id,
            publication_id=publication_id,
            portal_invitation_id=portal_invitation_id,
        ):
            raise NotificationIdempotencyConflict("NOTIFICATION_IDEMPOTENCY_KEY_REUSED")
        return existing, True
    notification_id = uuid.uuid4()
    notification = NotificationOutbox(
        id=notification_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        visit_id=visit_id,
        publication_id=publication_id,
        portal_invitation_id=portal_invitation_id,
        purpose=purpose,
        channel=channel,
        destination_ciphertext=field_codec.encrypt_text(
            clinic_id,
            "notification.destination",
            notification_id,
            normalized_destination,
        ),
        destination_masked=_mask_destination(normalized_destination, channel),
        template_key=template_key,
        payload_ciphertext=field_codec.encrypt_json(
            clinic_id, "notification.payload", notification_id, payload
        ),
        idempotency_key=digest,
        created_by_membership_id=created_by_membership_id,
    )
    try:
        with session.begin_nested():
            session.add(notification)
            session.flush()
    except IntegrityError:
        existing = session.exec(
            select(NotificationOutbox).where(
                NotificationOutbox.clinic_id == clinic_id,
                NotificationOutbox.idempotency_key == digest,
            )
        ).first()
        if existing is None:
            raise
        if not _same_notification_intent(
            existing,
            clinic_id=clinic_id,
            purpose=purpose,
            channel=channel,
            destination=normalized_destination,
            template_key=template_key,
            payload=payload,
            patient_id=patient_id,
            visit_id=visit_id,
            publication_id=publication_id,
            portal_invitation_id=portal_invitation_id,
        ):
            raise NotificationIdempotencyConflict("NOTIFICATION_IDEMPOTENCY_KEY_REUSED")
        return existing, True
    return notification, False


def dispatch_notification(
    session: Session, notification: NotificationOutbox
) -> NotificationOutbox:
    notification = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == notification.clinic_id,
            NotificationOutbox.id == notification.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if notification.state not in {"queued", "failed"}:
        return notification
    prior_attempts = session.exec(
        select(func.count())
        .select_from(NotificationAttempt)
        .where(
            NotificationAttempt.clinic_id == notification.clinic_id,
            NotificationAttempt.notification_id == notification.id,
        )
    ).one()
    if int(prior_attempts) >= settings.NOTIFICATION_MAX_ATTEMPTS:
        notification.state = "failed"
        notification.failed_at = notification.failed_at or get_datetime_utc()
        notification.updated_at = get_datetime_utc()
        session.add(notification)
        session.flush()
        return notification
    attempt_no = int(prior_attempts) + 1
    request_digest = hashlib.sha256()
    request_digest.update(notification.channel.encode())
    request_digest.update(b"\x00")
    request_digest.update(notification.template_key.encode())
    request_digest.update(b"\x00")
    request_digest.update(notification.destination_ciphertext)
    request_digest.update(b"\x00")
    request_digest.update(notification.payload_ciphertext)
    attempt = NotificationAttempt(
        clinic_id=notification.clinic_id,
        notification_id=notification.id,
        attempt_no=attempt_no,
        provider=_configured_provider_name(notification.channel),
        request_sha256=request_digest.hexdigest(),
    )
    session.add(attempt)
    session.flush()
    try:
        raw_payload = field_codec.decrypt_json(
            notification.clinic_id,
            "notification.payload",
            notification.id,
            notification.payload_ciphertext,
        )
        if not isinstance(raw_payload, dict):
            raise ValueError("NOTIFICATION_PAYLOAD_INVALID")
        submission = _provider(notification.channel).send(
            channel=notification.channel,
            destination=field_codec.decrypt_text(
                notification.clinic_id,
                "notification.destination",
                notification.id,
                notification.destination_ciphertext,
            ),
            template_key=notification.template_key,
            payload=raw_payload,
            # Creation idempotency prevents duplicate outbox rows. A deliberate
            # resend is a new provider submission and therefore needs its own
            # stable attempt key; otherwise an idempotent provider would return
            # the first failed message instead of sending the replacement.
            idempotency_key=(
                f"{notification.idempotency_key}:attempt:{attempt.attempt_no}"
            ),
            callback_url=twilio_status_callback_url(
                clinic_id=notification.clinic_id,
                notification_id=notification.id,
            ),
        )
        attempt.provider = submission.provider
        attempt.provider_message_id = submission.message_id
        attempt.status = "submitted"
        attempt.completed_at = get_datetime_utc()
        notification.state = "submitted"
        notification.submitted_at = get_datetime_utc()
        notification.failed_at = None
    except Exception:
        attempt.status = "failed"
        attempt.error_class = "provider"
        attempt.error_code = "NOTIFICATION_SUBMISSION_FAILED"
        attempt.completed_at = get_datetime_utc()
        notification.state = "failed"
        notification.failed_at = get_datetime_utc()
        retry_index = min(
            max(attempt_no - 1, 0), len(_NOTIFICATION_RETRY_DELAYS_SECONDS) - 1
        )
        notification.available_at = get_datetime_utc() + timedelta(
            seconds=_NOTIFICATION_RETRY_DELAYS_SECONDS[retry_index]
        )
    notification.updated_at = get_datetime_utc()
    session.add(attempt)
    session.add(notification)
    session.flush()
    return notification


def dispatch_due_notifications(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    limit: int = 25,
) -> int:
    """Claim and submit due outbox records for one already-bound clinic.

    The outbox row is created in the same transaction as the clinical action;
    this dispatcher is deliberately idempotent and uses row locks so multiple
    workers cannot submit the same queued intent concurrently.
    """

    now = get_datetime_utc()
    due = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == clinic_id,
            col(NotificationOutbox.state).in_(["queued", "failed"]),
            NotificationOutbox.available_at <= now,
        )
        .order_by(col(NotificationOutbox.available_at), col(NotificationOutbox.id))
        .limit(max(1, min(limit, 100)))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for notification in due:
        dispatch_notification(session, notification)
    return len(due)


def recover_stale_notifications(
    session: Session, *, clinic_id: uuid.UUID, limit: int = 100
) -> int:
    """Move callback-silent submissions back to the retryable failed worklist."""

    now = get_datetime_utc()
    cutoff = now - timedelta(seconds=settings.NOTIFICATION_SUBMITTED_STALE_SECONDS)
    stale = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == clinic_id,
            NotificationOutbox.state == "submitted",
            col(NotificationOutbox.submitted_at) < cutoff,
        )
        .order_by(col(NotificationOutbox.submitted_at), col(NotificationOutbox.id))
        .limit(max(1, min(limit, 500)))
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for notification in stale:
        notification.state = "failed"
        notification.failed_at = now
        notification.available_at = now
        notification.updated_at = now
        session.add(notification)
    session.flush()
    return len(stale)


def canonical_receipt_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def canonical_receipt_timestamp(value: datetime) -> str:
    """Return the provider-signing timestamp in one RFC 3339 UTC form."""

    if value.utcoffset() is None:
        raise ValueError("NOTIFICATION_RECEIPT_TIMESTAMP_NAIVE")
    return value.astimezone(UTC).isoformat()


def receipt_signature(payload: dict[str, object]) -> str:
    secret = settings.NOTIFICATION_WEBHOOK_SECRET
    return hmac.new(
        secret.encode(), canonical_receipt_payload(payload), hashlib.sha256
    ).hexdigest()


def apply_receipt(
    session: Session,
    *,
    notification: NotificationOutbox,
    provider: str,
    provider_event_id: str,
    provider_message_id: str,
    event_type: str,
    occurred_at: datetime,
    signature: str,
) -> NotificationOutbox:
    payload: dict[str, object] = {
        "notification_id": str(notification.id),
        "provider": provider,
        "provider_event_id": provider_event_id,
        "provider_message_id": provider_message_id,
        "event_type": event_type,
        "occurred_at": canonical_receipt_timestamp(occurred_at),
    }
    if not hmac.compare_digest(receipt_signature(payload), signature):
        raise ValueError("NOTIFICATION_RECEIPT_SIGNATURE_INVALID")
    payload_sha256 = hashlib.sha256(canonical_receipt_payload(payload)).hexdigest()
    notification = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == notification.clinic_id,
            NotificationOutbox.id == notification.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    existing = session.exec(
        select(NotificationReceipt).where(
            NotificationReceipt.clinic_id == notification.clinic_id,
            NotificationReceipt.provider == provider,
            NotificationReceipt.provider_event_id == provider_event_id,
        )
    ).first()
    if existing is not None:
        if (
            existing.notification_id != notification.id
            or existing.provider_message_id != provider_message_id
            or existing.event_type != event_type
            or existing.payload_sha256 != payload_sha256
        ):
            raise ValueError("NOTIFICATION_RECEIPT_EVENT_CONFLICT")
        return notification
    latest_attempt = session.exec(
        select(NotificationAttempt)
        .where(
            NotificationAttempt.clinic_id == notification.clinic_id,
            NotificationAttempt.notification_id == notification.id,
        )
        .order_by(col(NotificationAttempt.attempt_no).desc())
        .limit(1)
    ).first()
    if (
        latest_attempt is None
        or latest_attempt.provider != provider
        or latest_attempt.provider_message_id != provider_message_id
        or latest_attempt.status != "submitted"
    ):
        raise ValueError("NOTIFICATION_RECEIPT_ATTEMPT_INVALID")
    receipt = NotificationReceipt(
        clinic_id=notification.clinic_id,
        notification_id=notification.id,
        provider=provider,
        provider_event_id=provider_event_id,
        provider_message_id=provider_message_id,
        event_type=event_type,
        signature_verified=True,
        payload_sha256=payload_sha256,
        occurred_at=occurred_at,
    )
    session.add(receipt)
    now = get_datetime_utc()
    if event_type == "submitted" and notification.state in {"queued", "failed"}:
        notification.state = "submitted"
        notification.submitted_at = occurred_at
        notification.failed_at = None
    elif event_type == "delivered" and notification.state in {
        "submitted",
        "failed",
        "delivered",
    }:
        notification.state = "delivered"
        notification.delivered_at = occurred_at
    elif event_type in {"failed", "bounced", "undeliverable"} and (
        notification.state in {"queued", "submitted", "failed"}
    ):
        notification.state = "failed"
        notification.failed_at = occurred_at
        retry_index = min(
            max(latest_attempt.attempt_no - 1, 0),
            len(_NOTIFICATION_RETRY_DELAYS_SECONDS) - 1,
        )
        notification.available_at = now + timedelta(
            seconds=_NOTIFICATION_RETRY_DELAYS_SECONDS[retry_index]
        )
    elif event_type == "acknowledged" and notification.state in {
        "submitted",
        "delivered",
        "acknowledged",
    }:
        notification.state = "acknowledged"
        notification.acknowledged_at = occurred_at
    notification.updated_at = now
    session.add(notification)
    session.flush()
    return notification
