"""Transactional notification lifecycle and signed provider callbacks."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import text
from sqlmodel import Field, SQLModel, col, select
from sqlmodel._compat import SQLModelConfig
from twilio.request_validator import RequestValidator  # type: ignore[import-untyped]

from app.api.deps import CurrentContext, SessionDep
from app.core.config import settings
from app.core.db import set_rls_actor, set_rls_clinic
from app.core.field_crypto import field_codec
from app.models import (
    AuditEvent,
    ClinicMembership,
    NotificationAttempt,
    NotificationAttemptPublic,
    NotificationChannel,
    NotificationOutbox,
    NotificationPublic,
    NotificationReceipt,
    NotificationReceiptPublic,
    NotificationResendRequest,
    NotificationState,
    PatientAccessCredential,
    PatientOTPChallenge,
    PatientPortalInvitation,
    PatientVisit,
    User,
    get_datetime_utc,
)
from app.services.messaging import (
    NotificationChannelUnavailable,
    NotificationIdempotencyConflict,
    apply_receipt,
    canonical_receipt_timestamp,
    dispatch_notification,
    queue_notification,
    receipt_signature,
    recover_stale_notifications,
    twilio_status_callback_url,
    validate_notification_destination,
)
from app.services.nightingale import get_patient

router = APIRouter(tags=["notifications"])
_TWILIO_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{2,100}$")
_TWILIO_ERROR_CODE = re.compile(r"^[0-9]{1,10}$")


class AppointmentDeliveryCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    channel: Literal["email", "sms", "whatsapp"]
    destination: str = Field(min_length=3, max_length=320)
    scheduled_for: datetime | None = None


class ProviderCallbackCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    notification_id: uuid.UUID
    provider_event_id: str = Field(min_length=1, max_length=200)
    provider_message_id: str = Field(min_length=1, max_length=200)
    event_type: Literal[
        "submitted",
        "delivered",
        "failed",
        "bounced",
        "undeliverable",
        "acknowledged",
    ]
    occurred_at: datetime


def _require_delivery_role(context: CurrentContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical delivery role required")


def _rebind_delivery_actor(session: SessionDep, context: CurrentContext) -> None:
    set_rls_clinic(session, context.clinic_id)
    set_rls_actor(
        session,
        context.user_id,
        role=context.role,
        patient_id=context.linked_patient_id,
    )


def _validate_aware_datetime(
    value: datetime | None, *, detail: str = "Scheduled delivery needs a timezone"
) -> None:
    if value is not None and value.utcoffset() is None:
        raise HTTPException(status_code=422, detail=detail)


def _channel_error(exc: NotificationChannelUnavailable) -> HTTPException:
    code = str(exc)
    status_code = 422 if code.endswith("_INVALID") else 503
    return HTTPException(status_code=status_code, detail={"code": code})


def _twilio_receipt_event(params: dict[str, str]) -> str:
    status = (
        (
            params.get("MessageStatus")
            or params.get("SmsStatus")
            or params.get("EventType")
            or ""
        )
        .strip()
        .lower()
    )
    error_code = params.get("ErrorCode", "").strip()
    if error_code or status in {"failed", "undelivered"}:
        return "undeliverable"
    if status == "read":
        return "acknowledged"
    if status == "delivered":
        return "delivered"
    return "submitted"


def _twilio_provider_event_id(params: dict[str, str]) -> str:
    event_sid = params.get("EventSid", "").strip()
    if _TWILIO_IDENTIFIER.fullmatch(event_sid):
        return event_sid
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return f"twilio-{hashlib.sha256(canonical.encode()).hexdigest()}"


def _masked_destination(destination: str, channel: str) -> str:
    value = destination.strip()
    if channel == "email" and "@" in value:
        local, domain = value.rsplit("@", 1)
        return f"{local[:1]}***@{domain}"
    digits = "".join(character for character in value if character.isdigit())
    return f"***{digits[-4:]}" if digits else "***"


def _bind_callback_worker(session: SessionDep, clinic_id: uuid.UUID) -> None:
    """Bind the clinic's live service identity after signature verification."""

    if session.get_bind().dialect.name == "postgresql":
        row = (
            session.connection()
            .execute(
                text("SELECT * FROM app_lookup_clinic_worker(:clinic_id)"),
                {"clinic_id": clinic_id},
            )
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=503, detail="Callback worker unavailable")
        user_id = uuid.UUID(str(row.user_id))
    else:
        worker = session.exec(
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
        if worker is None:
            raise HTTPException(status_code=503, detail="Callback worker unavailable")
        _membership, user = worker
        user_id = user.id
    set_rls_clinic(session, clinic_id)
    set_rls_actor(session, user_id, role="worker")


def _notification(
    session: SessionDep, context: CurrentContext, notification_id: uuid.UUID
) -> NotificationOutbox:
    notification = session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.clinic_id == context.clinic_id,
            NotificationOutbox.id == notification_id,
        )
    ).first()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if (
        context.role == "patient"
        and notification.patient_id != context.linked_patient_id
    ):
        raise HTTPException(status_code=404, detail="Notification not found")
    return notification


def _public(
    session: SessionDep, notification: NotificationOutbox
) -> NotificationPublic:
    attempts = session.exec(
        select(NotificationAttempt)
        .where(
            NotificationAttempt.clinic_id == notification.clinic_id,
            NotificationAttempt.notification_id == notification.id,
        )
        .order_by(col(NotificationAttempt.attempt_no))
    ).all()
    receipts = session.exec(
        select(NotificationReceipt)
        .where(
            NotificationReceipt.clinic_id == notification.clinic_id,
            NotificationReceipt.notification_id == notification.id,
        )
        .order_by(col(NotificationReceipt.received_at))
    ).all()
    return NotificationPublic(
        id=notification.id,
        patient_id=notification.patient_id,
        visit_id=notification.visit_id,
        publication_id=notification.publication_id,
        portal_invitation_id=notification.portal_invitation_id,
        purpose=notification.purpose,
        channel=cast(NotificationChannel, notification.channel),
        destination_masked=notification.destination_masked,
        state=cast(NotificationState, notification.state),
        available_at=notification.available_at,
        submitted_at=notification.submitted_at,
        delivered_at=notification.delivered_at,
        failed_at=notification.failed_at,
        acknowledged_at=notification.acknowledged_at,
        revoked_at=notification.revoked_at,
        created_at=notification.created_at,
        updated_at=notification.updated_at,
        attempt_count=len(attempts),
        attempts=[NotificationAttemptPublic.model_validate(row) for row in attempts],
        receipts=[NotificationReceiptPublic.model_validate(row) for row in receipts],
    )


@router.get(
    "/patients/{patient_id}/visits/{visit_id}/notifications",
    response_model=list[NotificationPublic],
)
def list_appointment_notifications(
    patient_id: uuid.UUID,
    visit_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> list[NotificationPublic]:
    if context.role not in {"patient", "staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Role cannot view notifications")
    get_patient(session, context, patient_id)
    visit = session.exec(
        select(PatientVisit).where(
            PatientVisit.clinic_id == context.clinic_id,
            PatientVisit.patient_id == patient_id,
            PatientVisit.id == visit_id,
        )
    ).first()
    if visit is None:
        raise HTTPException(status_code=404, detail="Visit not found")
    notifications = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == context.clinic_id,
            NotificationOutbox.patient_id == patient_id,
            NotificationOutbox.visit_id == visit_id,
            NotificationOutbox.purpose == "appointment",
        )
        .order_by(col(NotificationOutbox.created_at).desc())
    ).all()
    return [_public(session, item) for item in notifications]


@router.post(
    "/patients/{patient_id}/visits/{visit_id}/notifications",
    response_model=NotificationPublic,
    status_code=201,
)
def create_appointment_notification(
    patient_id: uuid.UUID,
    visit_id: uuid.UUID,
    body: AppointmentDeliveryCreate,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=200)
    ] = None,
) -> NotificationPublic:
    _require_delivery_role(context)
    get_patient(session, context, patient_id)
    visit = session.exec(
        select(PatientVisit).where(
            PatientVisit.clinic_id == context.clinic_id,
            PatientVisit.patient_id == patient_id,
            PatientVisit.id == visit_id,
        )
    ).first()
    if visit is None:
        raise HTTPException(status_code=404, detail="Visit not found")
    _validate_aware_datetime(body.scheduled_for)
    if body.scheduled_for is not None and body.scheduled_for < get_datetime_utc():
        raise HTTPException(status_code=422, detail="Scheduled delivery is in the past")
    key = idempotency_key or str(uuid.uuid4())
    try:
        notification, replay = queue_notification(
            session,
            clinic_id=context.clinic_id,
            patient_id=patient_id,
            visit_id=visit_id,
            purpose="appointment",
            channel=body.channel,
            destination=body.destination,
            template_key="appointment-v1",
            payload={
                "visit_id": str(visit.id),
                "scheduled_at": visit.scheduled_at.isoformat(),
                "visit_type": visit.visit_type,
                "delivery_scheduled_for": (
                    body.scheduled_for.isoformat() if body.scheduled_for else None
                ),
            },
            idempotency_key=f"appointment:{patient_id}:{visit_id}:{key}",
            created_by_membership_id=context.membership.id,
        )
    except NotificationIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409, detail="Idempotency key was reused for another delivery"
        ) from exc
    except NotificationChannelUnavailable as exc:
        raise _channel_error(exc) from exc
    if body.scheduled_for is not None:
        notification.available_at = body.scheduled_for
        session.add(notification)
    if not replay:
        session.add(
            AuditEvent(
                clinic_id=context.clinic_id,
                actor_id=context.user_id,
                action="notification.appointment_queued",
                resource_type="notification",
                resource_id=notification.id,
                reason_code="appointment_delivery",
                metadata_json={"channel": body.channel},
            )
        )
    notification_id = notification.id
    session.commit()
    _rebind_delivery_actor(session, context)
    persisted_notification = session.get(NotificationOutbox, notification_id)
    if persisted_notification is None:
        raise HTTPException(status_code=409, detail="Notification unavailable")
    if persisted_notification.available_at <= get_datetime_utc():
        dispatch_notification(session, persisted_notification)
        session.commit()
        _rebind_delivery_actor(session, context)
    return _public(session, persisted_notification)


@router.get("/notifications/worklist", response_model=list[NotificationPublic])
def notification_delivery_worklist(
    session: SessionDep,
    context: CurrentContext,
    state: Literal["attention", "queued", "submitted", "failed"] = "attention",
    limit: int = Query(default=50, ge=1, le=200),
) -> list[NotificationPublic]:
    """Expose retryable and callback-silent delivery work to clinic staff."""

    _require_delivery_role(context)
    recover_stale_notifications(session, clinic_id=context.clinic_id, limit=limit)
    session.commit()
    _rebind_delivery_actor(session, context)
    states = ["queued", "failed"] if state == "attention" else [state]
    notifications = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == context.clinic_id,
            col(NotificationOutbox.state).in_(states),
        )
        .order_by(
            col(NotificationOutbox.available_at),
            col(NotificationOutbox.updated_at).desc(),
        )
        .limit(limit)
    ).all()
    return [_public(session, item) for item in notifications]


@router.get("/notifications/{notification_id}", response_model=NotificationPublic)
def read_notification(
    notification_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> NotificationPublic:
    return _public(session, _notification(session, context, notification_id))


@router.post(
    "/notifications/{notification_id}/resend", response_model=NotificationPublic
)
def resend_notification(
    notification_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    body: NotificationResendRequest | None = None,
) -> NotificationPublic:
    _require_delivery_role(context)
    notification = _notification(session, context, notification_id)
    notification = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == context.clinic_id,
            NotificationOutbox.id == notification.id,
        )
        .with_for_update()
    ).one()
    if notification.state != "failed":
        raise HTTPException(
            status_code=409, detail="Only failed delivery can be resent"
        )
    if body is not None and body.channel is not None and body.destination is None:
        if body.channel != notification.channel:
            raise HTTPException(
                status_code=422,
                detail="Changing delivery channel requires a destination",
            )
    selected_channel: str
    if body is not None and body.channel is not None:
        selected_channel = body.channel
    else:
        selected_channel = notification.channel
    if body is not None and body.destination is not None:
        selected_destination = body.destination
    else:
        selected_destination = field_codec.decrypt_text(
            context.clinic_id,
            "notification.destination",
            notification.id,
            notification.destination_ciphertext,
        )
    try:
        normalized_destination = validate_notification_destination(
            session,
            clinic_id=context.clinic_id,
            channel=selected_channel,
            destination=selected_destination,
        )
    except NotificationChannelUnavailable as exc:
        raise _channel_error(exc) from exc
    notification.channel = selected_channel
    if body is not None and body.destination is not None:
        notification.destination_ciphertext = field_codec.encrypt_text(
            context.clinic_id,
            "notification.destination",
            notification.id,
            normalized_destination,
        )
        notification.destination_masked = _masked_destination(
            normalized_destination,
            selected_channel,
        )
    notification.state = "queued"
    notification.failed_at = None
    notification.available_at = get_datetime_utc()
    notification.updated_at = get_datetime_utc()
    session.add(notification)
    session.add(
        AuditEvent(
            clinic_id=context.clinic_id,
            actor_id=context.user_id,
            action="notification.resent",
            resource_type="notification",
            resource_id=notification.id,
            reason_code="delivery_retry_requested",
            metadata_json={},
        )
    )
    session.commit()
    _rebind_delivery_actor(session, context)
    dispatch_notification(session, notification)
    session.commit()
    _rebind_delivery_actor(session, context)
    return _public(session, notification)


@router.post(
    "/notifications/{notification_id}/revoke", response_model=NotificationPublic
)
def revoke_notification(
    notification_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> NotificationPublic:
    _require_delivery_role(context)
    notification = _notification(session, context, notification_id)
    notification = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == context.clinic_id,
            NotificationOutbox.id == notification.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    # A delivered message cannot be recalled from the recipient's handset, so
    # marking it revoked would be a false record.  Enrollment and one-time-code
    # deliveries are the exception: the message is spent but the token it
    # carries is still live, and revoking is how that token is invalidated
    # below.
    carries_live_token = (
        notification.portal_invitation_id is not None
        or notification.purpose in {"patient_enrollment", "patient_otp"}
    )
    if notification.state in {"acknowledged", "revoked"} or (
        notification.state == "delivered" and not carries_live_token
    ):
        raise HTTPException(status_code=409, detail="Delivery cannot be revoked")
    now = get_datetime_utc()
    notification.state = "revoked"
    notification.revoked_at = now
    notification.updated_at = now
    session.add(notification)
    invitation = (
        session.exec(
            select(PatientPortalInvitation)
            .where(
                PatientPortalInvitation.clinic_id == context.clinic_id,
                PatientPortalInvitation.id == notification.portal_invitation_id,
            )
            .with_for_update()
        ).first()
        if notification.portal_invitation_id is not None
        else None
    )
    if invitation is None and notification.purpose == "patient_enrollment":
        # Backward-compatible lookup for outbox rows created before the
        # addressable invitation foreign key existed. The encrypted payload is
        # read only inside this transaction and is never returned or logged.
        payload = field_codec.decrypt_json(
            context.clinic_id,
            "notification.payload",
            notification.id,
            notification.payload_ciphertext,
        )
        enrollment_token = payload.get("enrollment_token")
        if isinstance(enrollment_token, str):
            invitation = session.exec(
                select(PatientPortalInvitation)
                .where(
                    PatientPortalInvitation.clinic_id == context.clinic_id,
                    PatientPortalInvitation.token_hash
                    == hashlib.sha256(enrollment_token.encode()).hexdigest(),
                )
                .with_for_update()
            ).first()
    if invitation is not None and invitation.accepted_at is None:
        invitation.revoked_at = now
        session.add(invitation)
        credentials = session.exec(
            select(PatientAccessCredential)
            .where(
                PatientAccessCredential.clinic_id == context.clinic_id,
                PatientAccessCredential.invitation_id == invitation.id,
                col(PatientAccessCredential.is_active).is_(True),
                col(PatientAccessCredential.claim_code_used_at).is_(None),
            )
            .with_for_update()
        ).all()
        for credential in credentials:
            credential.is_active = False
            credential.revoked_at = now
            credential.updated_at = now
            credential.recovery_version += 1
            session.add(credential)
            challenges = session.exec(
                select(PatientOTPChallenge)
                .where(
                    PatientOTPChallenge.clinic_id == context.clinic_id,
                    PatientOTPChallenge.credential_id == credential.id,
                    col(PatientOTPChallenge.consumed_at).is_(None),
                    col(PatientOTPChallenge.revoked_at).is_(None),
                )
                .with_for_update()
            ).all()
            for challenge in challenges:
                challenge.revoked_at = now
                session.add(challenge)
    session.add(
        AuditEvent(
            clinic_id=context.clinic_id,
            actor_id=context.user_id,
            action="notification.revoked",
            resource_type="notification",
            resource_id=notification.id,
            reason_code="delivery_revoked",
            metadata_json={},
        )
    )
    session.commit()
    _rebind_delivery_actor(session, context)
    return _public(session, notification)


@router.post(
    "/notifications/{notification_id}/acknowledge",
    response_model=NotificationPublic,
)
def acknowledge_notification(
    notification_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> NotificationPublic:
    notification = _notification(session, context, notification_id)
    if context.role != "patient" and context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Role cannot acknowledge delivery")
    notification = session.exec(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.clinic_id == context.clinic_id,
            NotificationOutbox.id == notification.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if notification.state not in {"submitted", "delivered", "acknowledged"}:
        raise HTTPException(status_code=409, detail="Delivery cannot be acknowledged")
    if notification.state != "acknowledged":
        notification.state = "acknowledged"
        notification.acknowledged_at = get_datetime_utc()
        notification.updated_at = get_datetime_utc()
        session.add(notification)
        session.add(
            AuditEvent(
                clinic_id=context.clinic_id,
                actor_id=context.user_id,
                action="notification.acknowledged",
                resource_type="notification",
                resource_id=notification.id,
                reason_code="recipient_acknowledged",
                metadata_json={},
            )
        )
        session.commit()
        _rebind_delivery_actor(session, context)
    return _public(session, notification)


@router.post(
    "/notification-webhooks/{clinic_id}/{provider}",
    response_model=NotificationPublic,
)
def provider_callback(
    clinic_id: uuid.UUID,
    provider: str,
    body: ProviderCallbackCreate,
    session: SessionDep,
    signature: Annotated[str, Header(alias="X-Notification-Signature")],
) -> NotificationPublic:
    """Advance a delivery only after verifying the provider's signed event."""

    _validate_aware_datetime(
        body.occurred_at, detail="Callback timestamp needs a timezone"
    )
    canonical: dict[str, object] = {
        "notification_id": str(body.notification_id),
        "provider": provider,
        "provider_event_id": body.provider_event_id,
        "provider_message_id": body.provider_message_id,
        "event_type": body.event_type,
        "occurred_at": canonical_receipt_timestamp(body.occurred_at),
    }
    # Verify before opening any tenant context or selecting an outbox row.
    if not hmac.compare_digest(receipt_signature(canonical), signature):
        raise HTTPException(status_code=403, detail="Callback signature is invalid")
    _bind_callback_worker(session, clinic_id)
    notification = session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.clinic_id == clinic_id,
            NotificationOutbox.id == body.notification_id,
        )
    ).first()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    try:
        notification = apply_receipt(
            session,
            notification=notification,
            provider=provider,
            provider_event_id=body.provider_event_id,
            provider_message_id=body.provider_message_id,
            event_type=body.event_type,
            occurred_at=body.occurred_at,
            signature=signature,
        )
    except ValueError as exc:
        if str(exc) in {
            "NOTIFICATION_RECEIPT_ATTEMPT_INVALID",
            "NOTIFICATION_RECEIPT_EVENT_CONFLICT",
        }:
            raise HTTPException(
                status_code=409,
                detail="Callback conflicts with the active delivery attempt",
            ) from exc
        raise HTTPException(
            status_code=403, detail="Callback signature is invalid"
        ) from exc
    session.commit()
    _bind_callback_worker(session, clinic_id)
    return _public(session, notification)


@router.post(
    "/notification-webhooks/twilio/{clinic_id}/{notification_id}",
    response_model=NotificationPublic,
)
async def twilio_provider_callback(
    clinic_id: uuid.UUID,
    notification_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    signature: Annotated[
        str | None, Header(alias="X-Twilio-Signature", max_length=256)
    ] = None,
) -> NotificationPublic:
    """Validate Twilio's exact signed URL/form envelope before tenant lookup."""

    expected_url = twilio_status_callback_url(
        clinic_id=clinic_id, notification_id=notification_id
    )
    if expected_url is None or not settings.TWILIO_AUTH_TOKEN or signature is None:
        raise HTTPException(status_code=403, detail="Twilio callback is invalid")
    raw_form = await request.form()
    params = {str(key): str(value) for key, value in raw_form.multi_items()}
    if not RequestValidator(settings.TWILIO_AUTH_TOKEN).validate(
        expected_url, params, signature
    ):
        raise HTTPException(status_code=403, detail="Twilio callback is invalid")
    message_id = params.get("MessageSid", "").strip()
    if not _TWILIO_IDENTIFIER.fullmatch(message_id):
        raise HTTPException(status_code=422, detail="Twilio callback is invalid")
    event_type = _twilio_receipt_event(params)
    provider_event_id = _twilio_provider_event_id(params)

    _bind_callback_worker(session, clinic_id)
    notification = session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.clinic_id == clinic_id,
            NotificationOutbox.id == notification_id,
        )
    ).first()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    latest_attempt = session.exec(
        select(NotificationAttempt)
        .where(
            NotificationAttempt.clinic_id == clinic_id,
            NotificationAttempt.notification_id == notification.id,
        )
        .order_by(col(NotificationAttempt.attempt_no).desc())
        .limit(1)
    ).first()
    if latest_attempt is None or not latest_attempt.provider.startswith("twilio-"):
        raise HTTPException(status_code=409, detail="Twilio callback attempt mismatch")
    existing = session.exec(
        select(NotificationReceipt).where(
            NotificationReceipt.clinic_id == clinic_id,
            NotificationReceipt.provider == latest_attempt.provider,
            NotificationReceipt.provider_event_id == provider_event_id,
        )
    ).first()
    occurred_at = existing.occurred_at if existing is not None else get_datetime_utc()
    canonical: dict[str, object] = {
        "notification_id": str(notification.id),
        "provider": latest_attempt.provider,
        "provider_event_id": provider_event_id,
        "provider_message_id": message_id,
        "event_type": event_type,
        "occurred_at": canonical_receipt_timestamp(occurred_at),
    }
    try:
        notification = apply_receipt(
            session,
            notification=notification,
            provider=latest_attempt.provider,
            provider_event_id=provider_event_id,
            provider_message_id=message_id,
            event_type=event_type,
            occurred_at=occurred_at,
            # apply_receipt has a single verified-envelope gate. Re-sign the
            # normalized event only after Twilio's official validator succeeded.
            signature=receipt_signature(canonical),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Twilio callback conflicts with delivery attempt"
        ) from exc
    error_code = params.get("ErrorCode", "").strip()
    if event_type == "undeliverable" and _TWILIO_ERROR_CODE.fullmatch(error_code):
        latest_attempt.error_class = "provider"
        latest_attempt.error_code = f"TWILIO_{error_code}"
        session.add(latest_attempt)
    session.commit()
    _bind_callback_worker(session, clinic_id)
    return _public(session, notification)
