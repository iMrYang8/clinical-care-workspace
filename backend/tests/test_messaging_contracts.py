from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    ClinicMembership,
    ClinicOperationalSetting,
    NotificationAttempt,
    NotificationOutbox,
    User,
)
from app.services.messaging import (
    DeterministicEmailProvider,
    DeterministicNotificationProvider,
    DeterministicSMSProvider,
    DeterministicWhatsAppProvider,
    NotificationChannelCapability,
    NotificationChannelUnavailable,
    NotificationIdempotencyConflict,
    SMTPNotificationProvider,
    TwilioNotificationProvider,
    _configured_provider_name,
    _provider,
    _same_notification_intent,
    apply_receipt,
    bind_notification_worker,
    canonical_receipt_timestamp,
    clear_deterministic_inbox,
    deterministic_inbox_messages,
    dispatch_due_notifications,
    dispatch_notification,
    normalize_destination,
    notification_channel_capabilities,
    queue_notification,
    receipt_signature,
    validate_notification_destination,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("provider", "channel", "destination"),
    [
        (DeterministicEmailProvider(), "email", "fixture@example.com"),
        (DeterministicSMSProvider(), "sms", "+6591234567"),
        (DeterministicWhatsAppProvider(), "whatsapp", "+6598765432"),
    ],
)
def test_deterministic_channel_inboxes_are_observable_and_idempotent(
    provider: Any, channel: str, destination: str
) -> None:
    clear_deterministic_inbox()
    kwargs = {
        "channel": channel,
        "destination": destination,
        "template_key": "appointment-v1",
        "payload": {"visit_id": "synthetic"},
        "idempotency_key": "same-safe-key",
    }
    first = provider.send(**kwargs)
    second = provider.send(**kwargs)

    assert first.message_id == second.message_id
    assert first.state == "submitted"
    messages = deterministic_inbox_messages(channel=channel, destination=destination)
    assert len(messages) == 1
    assert messages[0].message_id == first.message_id
    assert messages[0].template_key == "appointment-v1"
    assert messages[0].payload == {"visit_id": "synthetic"}


def test_smtp_provider_contract_submits_rendered_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert (host, port, timeout) == (
                "smtp.fixture.invalid",
                2525,
                settings.NOTIFICATION_REQUEST_TIMEOUT_SECONDS,
            )

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def starttls(self) -> None:
            return None

        def login(self, user: str, password: str) -> None:
            assert (user, password) == ("fixture-user", "fixture-password")

        def send_message(self, message: EmailMessage) -> None:
            sent.append(message)

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.fixture.invalid")
    monkeypatch.setattr(settings, "SMTP_PORT", 2525)
    monkeypatch.setattr(settings, "SMTP_TLS", True)
    monkeypatch.setattr(settings, "SMTP_SSL", False)
    monkeypatch.setattr(settings, "SMTP_USER", "fixture-user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "fixture-password")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "sender@example.com")
    monkeypatch.setattr("app.services.messaging.smtplib.SMTP", FakeSMTP)

    result = SMTPNotificationProvider().send(
        channel="email",
        destination="recipient@example.com",
        template_key="staff-invitation-v1",
        payload={"clinic_code": "FIXTURE", "invitation_token": "TOKEN"},
        idempotency_key="smtp-contract-idempotency-key",
    )

    assert result.provider == "smtp"
    assert len(sent) == 1
    assert sent[0]["To"] == "recipient@example.com"
    assert sent[0]["X-Nightingale-Idempotency-Key"]
    assert "TOKEN" in sent[0].get_content()


def test_twilio_sms_and_whatsapp_provider_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        request = httpx.Request("POST", url)
        return httpx.Response(201, request=request, json={"sid": f"SM{len(calls)}"})

    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_fixture")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "fixture-token")
    monkeypatch.setattr(settings, "TWILIO_SMS_FROM", "+6590000000")
    monkeypatch.setattr(settings, "TWILIO_WHATSAPP_FROM", "+6590000001")
    monkeypatch.setattr("app.services.messaging.httpx.post", fake_post)

    sms = TwilioNotificationProvider(channel="sms").send(
        channel="sms",
        destination="+6591111111",
        template_key="patient-otp-v1",
        payload={"otp": "123456"},
        idempotency_key="sms-idempotency-key",
        callback_url="https://notifications.example.com/sms-status",
    )
    whatsapp = TwilioNotificationProvider(channel="whatsapp").send(
        channel="whatsapp",
        destination="+6592222222",
        template_key="appointment-v1",
        payload={"visit_id": "fixture"},
        idempotency_key="whatsapp-idempotency-key",
        callback_url="https://notifications.example.com/whatsapp-status",
    )

    assert (sms.provider, whatsapp.provider) == ("twilio-sms", "twilio-whatsapp")
    assert calls[0]["data"]["To"] == "+6591111111"  # type: ignore[index]
    assert calls[1]["data"]["To"] == "whatsapp:+6592222222"  # type: ignore[index]
    assert calls[1]["data"]["From"] == "whatsapp:+6590000001"  # type: ignore[index]
    assert (
        calls[0]["data"]["StatusCallback"]  # type: ignore[index]
        == "https://notifications.example.com/sms-status"
    )
    assert calls[0]["headers"] == {"Idempotency-Key": "sms-idempotency-key"}


def test_receipt_signature_uses_only_the_independent_callback_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "notification_id": "00000000-0000-0000-0000-000000000123",
        "provider": "deterministic",
        "provider_event_id": "event-1",
        "provider_message_id": "message-1",
        "event_type": "delivered",
        "occurred_at": "2026-09-01T00:00:00+00:00",
    }
    signature = receipt_signature(payload)
    monkeypatch.setattr(settings, "SECRET_KEY", "rotated-jwt-secret")

    assert len(signature) == 64
    assert signature == receipt_signature(payload)
    assert signature != receipt_signature(payload | {"event_type": "failed"})


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def first(self) -> object:
        return self.value

    def one(self) -> object:
        return self.value

    def one_or_none(self) -> object:
        return self.value

    def all(self) -> list[object]:
        return list(self.value) if isinstance(self.value, list) else []


def _outbox(
    *,
    clinic_id: uuid.UUID | None = None,
    state: str = "queued",
    purpose: str = "appointment",
    channel: str = "email",
    destination: str = "fixture@example.com",
    payload: dict[str, object] | None = None,
) -> NotificationOutbox:
    scoped_clinic_id = clinic_id or uuid.uuid4()
    notification_id = uuid.uuid4()
    body = payload or {"visit_id": "fixture"}
    return NotificationOutbox(
        id=notification_id,
        clinic_id=scoped_clinic_id,
        purpose=purpose,
        channel=channel,
        destination_ciphertext=field_codec.encrypt_text(
            scoped_clinic_id,
            "notification.destination",
            notification_id,
            destination,
        ),
        destination_masked="f***@example.com",
        template_key="appointment-v1",
        payload_ciphertext=field_codec.encrypt_json(
            scoped_clinic_id,
            "notification.payload",
            notification_id,
            body,
        ),
        idempotency_key="a" * 64,
        state=state,
    )


def test_provider_guards_fail_closed_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="NOTIFICATION_CHANNEL_PROVIDER_MISMATCH"):
        DeterministicEmailProvider().send(
            channel="sms",
            destination="+6591234567",
            template_key="patient-otp-v1",
            payload={"otp": "123456"},
            idempotency_key="wrong-provider-channel",
        )

    monkeypatch.setattr(settings, "SMTP_HOST", None)
    with pytest.raises(RuntimeError, match="SMTP_NOTIFICATION_UNAVAILABLE"):
        SMTPNotificationProvider().send(
            channel="email",
            destination="fixture@example.com",
            template_key="appointment-v1",
            payload={},
            idempotency_key="smtp-unavailable",
        )

    with pytest.raises(ValueError, match="TWILIO_CHANNEL_INVALID"):
        TwilioNotificationProvider(channel="email")
    sms = TwilioNotificationProvider(channel="sms")
    with pytest.raises(ValueError, match="NOTIFICATION_CHANNEL_PROVIDER_MISMATCH"):
        sms.send(
            channel="whatsapp",
            destination="+6591234567",
            template_key="patient-otp-v1",
            payload={},
            idempotency_key="twilio-channel-mismatch",
            callback_url="https://notifications.example/callback",
        )
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", None)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    with pytest.raises(RuntimeError, match="TWILIO_NOTIFICATION_UNAVAILABLE"):
        sms.send(
            channel="sms",
            destination="+6591234567",
            template_key="patient-otp-v1",
            payload={},
            idempotency_key="twilio-no-credentials",
            callback_url="https://notifications.example/callback",
        )
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_fixture")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "fixture-token")
    monkeypatch.setattr(settings, "TWILIO_SMS_FROM", None)
    with pytest.raises(RuntimeError, match="TWILIO_NOTIFICATION_UNAVAILABLE"):
        sms.send(
            channel="sms",
            destination="+6591234567",
            template_key="patient-otp-v1",
            payload={},
            idempotency_key="twilio-no-sender",
            callback_url="https://notifications.example/callback",
        )
    monkeypatch.setattr(settings, "TWILIO_SMS_FROM", "+6590000000")
    with pytest.raises(RuntimeError, match="TWILIO_CALLBACK_URL_UNAVAILABLE"):
        sms.send(
            channel="sms",
            destination="+6591234567",
            template_key="patient-otp-v1",
            payload={},
            idempotency_key="twilio-no-callback",
        )

    def invalid_twilio_response(url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(201, request=httpx.Request("POST", url), json={})

    monkeypatch.setattr("app.services.messaging.httpx.post", invalid_twilio_response)
    with pytest.raises(RuntimeError, match="TWILIO_RESPONSE_INVALID"):
        sms.send(
            channel="sms",
            destination="+6591234567",
            template_key="patient-otp-v1",
            payload={},
            idempotency_key="twilio-invalid-response",
            callback_url="https://notifications.example/callback",
        )


def test_capabilities_registry_and_destination_normalization_cover_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "NOTIFICATION_EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(settings, "SMTP_HOST", None)
    monkeypatch.setattr(settings, "NOTIFICATION_SMS_PROVIDER", "twilio")
    monkeypatch.setattr(settings, "NOTIFICATION_WHATSAPP_PROVIDER", "twilio")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_fixture")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "fixture-token")
    monkeypatch.setattr(
        settings, "NOTIFICATION_PUBLIC_BASE_URL", "https://notifications.example"
    )
    monkeypatch.setattr(settings, "TWILIO_SMS_FROM", "+6590000000")
    monkeypatch.setattr(settings, "TWILIO_WHATSAPP_FROM", None)

    capabilities = notification_channel_capabilities()
    assert capabilities["email"].reason_code == "smtp_not_configured"
    assert capabilities["sms"].configured is True
    assert capabilities["whatsapp"].reason_code == "twilio_not_configured"
    assert isinstance(_provider("email"), SMTPNotificationProvider)
    assert isinstance(_provider("sms"), TwilioNotificationProvider)
    with pytest.raises(RuntimeError, match="NOTIFICATION_PROVIDER_UNAVAILABLE"):
        _provider("fax")
    monkeypatch.setattr(settings, "FASTAPI_ENV", "production")
    assert _configured_provider_name("portal") == "disabled"
    assert _configured_provider_name("fax") == "disabled"

    assert normalize_destination("USER@Example.COM", "email") == "user@example.com"
    assert normalize_destination("0065 9123-4567", "sms") == "+6591234567"
    assert normalize_destination("portal-user", "portal") == "portal-user"
    with pytest.raises(ValueError, match="NOTIFICATION_EMAIL_INVALID"):
        normalize_destination("Name <user@example.com>", "email")
    with pytest.raises(ValueError, match="NOTIFICATION_PHONE_INVALID"):
        normalize_destination("123", "sms")
    with pytest.raises(ValueError, match="NOTIFICATION_DESTINATION_INVALID"):
        normalize_destination("destination", "fax")


class _PolicySession:
    def __init__(self, operational: ClinicOperationalSetting | None) -> None:
        self.operational = operational

    def exec(self, _statement: object) -> _Result:
        return _Result(self.operational)


def test_destination_validation_enforces_syntax_clinic_policy_and_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    with pytest.raises(
        NotificationChannelUnavailable, match="NOTIFICATION_EMAIL_INVALID"
    ):
        validate_notification_destination(
            _PolicySession(None),  # type: ignore[arg-type]
            clinic_id=clinic_id,
            channel="email",
            destination="invalid",
        )
    policy = ClinicOperationalSetting(
        clinic_id=clinic_id,
        messaging_channels_json=["sms"],
    )
    with pytest.raises(
        NotificationChannelUnavailable,
        match="CLINIC_NOTIFICATION_CHANNEL_DISABLED",
    ):
        validate_notification_destination(
            _PolicySession(policy),  # type: ignore[arg-type]
            clinic_id=clinic_id,
            channel="email",
            destination="fixture@example.com",
        )
    monkeypatch.setattr(
        "app.services.messaging.notification_channel_capabilities",
        lambda: {
            "email": NotificationChannelCapability(
                channel="email",
                provider="disabled",
                configured=False,
                production_safe=False,
            )
        },
    )
    with pytest.raises(
        NotificationChannelUnavailable, match="NOTIFICATION_CHANNEL_UNAVAILABLE"
    ):
        validate_notification_destination(
            _PolicySession(None),  # type: ignore[arg-type]
            clinic_id=clinic_id,
            channel="email",
            destination="fixture@example.com",
        )


def test_notification_intent_comparison_decrypts_exact_destination_and_payload() -> (
    None
):
    notification = _outbox()
    payload = {"visit_id": "fixture"}
    assert _same_notification_intent(
        notification,
        clinic_id=notification.clinic_id,
        purpose="appointment",
        channel="email",
        destination="fixture@example.com",
        template_key="appointment-v1",
        payload=payload,
        patient_id=None,
        visit_id=None,
        publication_id=None,
        portal_invitation_id=None,
    )
    assert not _same_notification_intent(
        notification,
        clinic_id=notification.clinic_id,
        purpose="correction",
        channel="email",
        destination="fixture@example.com",
        template_key="appointment-v1",
        payload=payload,
        patient_id=None,
        visit_id=None,
        publication_id=None,
        portal_invitation_id=None,
    )
    # A reused idempotency key must not bind to a different portal invitation.
    assert not _same_notification_intent(
        notification,
        clinic_id=notification.clinic_id,
        purpose="appointment",
        channel="email",
        destination="fixture@example.com",
        template_key="appointment-v1",
        payload=payload,
        patient_id=None,
        visit_id=None,
        publication_id=None,
        portal_invitation_id=uuid.uuid4(),
    )


class _NestedTransaction(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _QueueRaceSession:
    def __init__(self, existing: NotificationOutbox | None) -> None:
        self.existing = existing
        self.exec_count = 0
        self.flush_count = 0

    def exec(self, _statement: object) -> _Result:
        self.exec_count += 1
        return _Result(None if self.exec_count == 1 else self.existing)

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()

    def add(self, _value: object) -> None:
        return None

    def flush(self) -> None:
        self.flush_count += 1
        if self.flush_count == 1:
            raise IntegrityError("INSERT", {}, Exception("duplicate"))


def test_queue_notification_recovers_matching_insert_race_and_rejects_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _outbox()
    monkeypatch.setattr(
        "app.services.messaging.validate_notification_destination",
        lambda *_args, **_kwargs: "fixture@example.com",
    )
    recovered, replay = queue_notification(
        _QueueRaceSession(existing),  # type: ignore[arg-type]
        clinic_id=existing.clinic_id,
        purpose="appointment",
        channel="email",
        destination="fixture@example.com",
        template_key="appointment-v1",
        payload={"visit_id": "fixture"},
        idempotency_key="race-key",
    )
    assert recovered is existing
    assert replay is True

    class _ExistingSession:
        def exec(self, _statement: object) -> _Result:
            return _Result(existing)

    direct_replay, direct_replayed = queue_notification(
        _ExistingSession(),  # type: ignore[arg-type]
        clinic_id=existing.clinic_id,
        purpose="appointment",
        channel="email",
        destination="fixture@example.com",
        template_key="appointment-v1",
        payload={"visit_id": "fixture"},
        idempotency_key="existing-key",
    )
    assert direct_replay is existing
    assert direct_replayed is True

    conflicting = _outbox(clinic_id=existing.clinic_id, purpose="correction")
    with pytest.raises(NotificationIdempotencyConflict):
        queue_notification(
            _QueueRaceSession(conflicting),  # type: ignore[arg-type]
            clinic_id=existing.clinic_id,
            purpose="appointment",
            channel="email",
            destination="fixture@example.com",
            template_key="appointment-v1",
            payload={"visit_id": "fixture"},
            idempotency_key="conflicting-race-key",
        )
    with pytest.raises(IntegrityError):
        queue_notification(
            _QueueRaceSession(None),  # type: ignore[arg-type]
            clinic_id=existing.clinic_id,
            purpose="appointment",
            channel="email",
            destination="fixture@example.com",
            template_key="appointment-v1",
            payload={"visit_id": "fixture"},
            idempotency_key="vanished-race-key",
        )


class _SequenceSession:
    def __init__(self, values: list[object]) -> None:
        self.values = list(values)
        self.added: list[object] = []
        self.flushes = 0

    def exec(self, _statement: object) -> _Result:
        return _Result(self.values.pop(0))

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        self.flushes += 1


def test_dispatch_guard_exhaustion_failure_and_due_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = _outbox(state="submitted")
    assert (
        dispatch_notification(
            _SequenceSession([submitted]),  # type: ignore[arg-type]
            submitted,
        )
        is submitted
    )

    exhausted = _outbox()
    exhausted_session = _SequenceSession(
        [exhausted, settings.NOTIFICATION_MAX_ATTEMPTS]
    )
    dispatch_notification(exhausted_session, exhausted)  # type: ignore[arg-type]
    assert exhausted.state == "failed"
    assert exhausted.failed_at is not None

    failing = _outbox()

    class _FailingProvider(DeterministicNotificationProvider):
        def send(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider offline")

    monkeypatch.setattr(
        "app.services.messaging._provider", lambda _channel: _FailingProvider()
    )
    failure_session = _SequenceSession([failing, 0])
    dispatch_notification(failure_session, failing)  # type: ignore[arg-type]
    assert failing.state == "failed"
    attempt = next(
        item for item in failure_session.added if isinstance(item, NotificationAttempt)
    )
    assert attempt.error_code == "NOTIFICATION_SUBMISSION_FAILED"

    invalid_payload = _outbox()
    monkeypatch.setattr(
        "app.services.messaging.field_codec.decrypt_json",
        lambda *_args, **_kwargs: ["not", "an", "object"],
    )
    invalid_payload_session = _SequenceSession([invalid_payload, 0])
    dispatch_notification(
        invalid_payload_session,  # type: ignore[arg-type]
        invalid_payload,
    )
    assert invalid_payload.state == "failed"

    due = [_outbox(), _outbox()]
    dispatched: list[NotificationOutbox] = []
    monkeypatch.setattr(
        "app.services.messaging.dispatch_notification",
        lambda _session, notification: dispatched.append(notification),
    )
    assert (
        dispatch_due_notifications(
            _SequenceSession([due]),  # type: ignore[arg-type]
            clinic_id=due[0].clinic_id,
            limit=1_000,
        )
        == 2
    )
    assert dispatched == due


def _receipt_session(
    notification: NotificationOutbox, attempt: NotificationAttempt
) -> _SequenceSession:
    return _SequenceSession([notification, None, attempt])


@pytest.mark.parametrize(
    ("initial_state", "event_type", "expected_state"),
    [
        ("failed", "submitted", "submitted"),
        ("delivered", "acknowledged", "acknowledged"),
    ],
)
def test_receipt_state_transitions_include_resubmission_and_acknowledgement(
    initial_state: str,
    event_type: str,
    expected_state: str,
) -> None:
    notification = _outbox(state=initial_state)
    attempt = NotificationAttempt(
        clinic_id=notification.clinic_id,
        notification_id=notification.id,
        attempt_no=1,
        provider="deterministic-email",
        provider_message_id="message-1",
        request_sha256="a" * 64,
        status="submitted",
    )
    occurred_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    payload: dict[str, object] = {
        "notification_id": str(notification.id),
        "provider": attempt.provider,
        "provider_event_id": f"event-{event_type}",
        "provider_message_id": attempt.provider_message_id,
        "event_type": event_type,
        "occurred_at": canonical_receipt_timestamp(occurred_at),
    }
    updated = apply_receipt(
        _receipt_session(notification, attempt),  # type: ignore[arg-type]
        notification=notification,
        provider=attempt.provider,
        provider_event_id=str(payload["provider_event_id"]),
        provider_message_id=str(attempt.provider_message_id),
        event_type=event_type,
        occurred_at=occurred_at,
        signature=receipt_signature(payload),
    )
    assert updated.state == expected_state
    if event_type == "submitted":
        assert updated.submitted_at == occurred_at
        assert updated.failed_at is None
    else:
        assert updated.acknowledged_at == occurred_at


def test_worker_binding_and_receipt_timestamp_signature_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BindSession:
        def __init__(self, dialect: str, value: object) -> None:
            self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
            self.result = _Result(value)

        def get_bind(self) -> object:
            return self.bind

        def connection(self) -> _BindSession:
            return self

        def execute(self, _statement: object, _params: object) -> _Result:
            return self.result

        def exec(self, _statement: object) -> _Result:
            return self.result

    assert not bind_notification_worker(
        _BindSession("postgresql", None),  # type: ignore[arg-type]
        uuid.uuid4(),
    )
    assert not bind_notification_worker(
        _BindSession("sqlite", None),  # type: ignore[arg-type]
        uuid.uuid4(),
    )
    clinic_id = uuid.uuid4()
    user = User(
        id=uuid.uuid4(),
        email="worker@example.com",
        account_kind="service",
        hashed_password="fixture",
    )
    membership = ClinicMembership(
        clinic_id=clinic_id,
        user_id=user.id,
        role="worker",
    )
    bound: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "app.services.messaging.set_rls_clinic",
        lambda *_args: bound.append(("clinic", *_args[1:])),
    )
    monkeypatch.setattr(
        "app.services.messaging.set_rls_actor",
        lambda *_args, **kwargs: bound.append(("actor", *_args[1:], kwargs["role"])),
    )
    assert bind_notification_worker(
        _BindSession("sqlite", (membership, user)),  # type: ignore[arg-type]
        clinic_id,
    )
    assert bound == [("clinic", clinic_id), ("actor", user.id, "worker")]

    with pytest.raises(ValueError, match="NOTIFICATION_RECEIPT_TIMESTAMP_NAIVE"):
        canonical_receipt_timestamp(datetime(2026, 9, 2, 12, 0))
    invalid = _outbox()
    with pytest.raises(ValueError, match="NOTIFICATION_RECEIPT_SIGNATURE_INVALID"):
        apply_receipt(
            _SequenceSession([]),  # type: ignore[arg-type]
            notification=invalid,
            provider="deterministic-email",
            provider_event_id="event-invalid",
            provider_message_id="message-invalid",
            event_type="delivered",
            occurred_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
            signature="invalid",
        )
