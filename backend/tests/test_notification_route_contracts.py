from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.deps import RequestContext
from app.api.routes.notifications import (
    AppointmentDeliveryCreate,
    ProviderCallbackCreate,
    _bind_callback_worker,
    _channel_error,
    _masked_destination,
    _notification,
    _twilio_provider_event_id,
    _twilio_receipt_event,
    acknowledge_notification,
    create_appointment_notification,
    list_appointment_notifications,
    provider_callback,
    read_notification,
    resend_notification,
    revoke_notification,
    twilio_provider_callback,
)
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    ClinicMembership,
    NotificationAttempt,
    NotificationOutbox,
    NotificationResendRequest,
    PatientVisit,
    User,
)
from app.services.messaging import (
    NotificationChannelUnavailable,
    canonical_receipt_timestamp,
    receipt_signature,
)

pytestmark = pytest.mark.unit


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


class _Session:
    def __init__(self, values: list[object] | None = None) -> None:
        self.values = list(values or [])
        self.added: list[object] = []
        self.commits = 0

    def exec(self, _statement: object) -> _Result:
        return _Result(self.values.pop(0))

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commits += 1


def _context(
    *,
    clinic_id: uuid.UUID | None = None,
    role: str = "staff",
    linked_patient_id: uuid.UUID | None = None,
) -> RequestContext:
    scoped_clinic_id = clinic_id or uuid.uuid4()
    account_kind = "patient" if role == "patient" else "staff"
    user = User(
        id=uuid.uuid4(),
        email=f"{role}-{uuid.uuid4()}@example.com",
        account_kind=account_kind,
        hashed_password="fixture",
    )
    membership = ClinicMembership(
        id=uuid.uuid4(),
        clinic_id=scoped_clinic_id,
        user_id=user.id,
        role=role,
    )
    return RequestContext(
        user=user,
        membership=membership,
        linked_patient_id=linked_patient_id,
    )


def _notification_model(
    *,
    clinic_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    visit_id: uuid.UUID | None = None,
    state: str = "queued",
    channel: str = "sms",
    destination: str = "+6591234567",
) -> NotificationOutbox:
    scoped_clinic_id = clinic_id or uuid.uuid4()
    notification_id = uuid.uuid4()
    return NotificationOutbox(
        id=notification_id,
        clinic_id=scoped_clinic_id,
        patient_id=patient_id,
        visit_id=visit_id,
        purpose="appointment",
        channel=channel,
        destination_ciphertext=field_codec.encrypt_text(
            scoped_clinic_id,
            "notification.destination",
            notification_id,
            destination,
        ),
        destination_masked="***4567",
        template_key="appointment-v1",
        payload_ciphertext=field_codec.encrypt_json(
            scoped_clinic_id,
            "notification.payload",
            notification_id,
            {"visit_id": str(visit_id) if visit_id else "fixture"},
        ),
        idempotency_key="a" * 64,
        state=state,
    )


def test_notification_helper_mappings_are_deterministic_and_phi_minimal() -> None:
    invalid = _channel_error(
        NotificationChannelUnavailable("NOTIFICATION_EMAIL_INVALID")
    )
    unavailable = _channel_error(
        NotificationChannelUnavailable("NOTIFICATION_CHANNEL_UNAVAILABLE")
    )
    assert (invalid.status_code, unavailable.status_code) == (422, 503)
    assert _twilio_receipt_event({"MessageStatus": "failed"}) == "undeliverable"
    assert _twilio_receipt_event({"SmsStatus": "read"}) == "acknowledged"
    assert _twilio_receipt_event({"EventType": "delivered"}) == "delivered"
    assert _twilio_receipt_event({}) == "submitted"
    assert _twilio_receipt_event({"ErrorCode": "30003"}) == "undeliverable"
    assert _twilio_provider_event_id({"EventSid": "EZ_fixture"}) == "EZ_fixture"
    fallback_event_id = _twilio_provider_event_id({"MessageStatus": "submitted"})
    assert fallback_event_id.startswith("twilio-")
    assert fallback_event_id == _twilio_provider_event_id(
        {"MessageStatus": "submitted"}
    )
    assert _masked_destination("person@example.com", "email") == "p***@example.com"
    assert _masked_destination("no-digits", "sms") == "***"


def test_callback_worker_binding_fails_closed_and_supports_sqlite_fixture(
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

    with pytest.raises(HTTPException) as missing_postgres:
        _bind_callback_worker(
            _BindSession("postgresql", None),  # type: ignore[arg-type]
            uuid.uuid4(),
        )
    assert missing_postgres.value.status_code == 503
    with pytest.raises(HTTPException) as missing_sqlite:
        _bind_callback_worker(
            _BindSession("sqlite", None),  # type: ignore[arg-type]
            uuid.uuid4(),
        )
    assert missing_sqlite.value.status_code == 503

    clinic_id = uuid.uuid4()
    worker = User(
        id=uuid.uuid4(),
        email="callback-worker@example.com",
        account_kind="service",
        hashed_password="fixture",
    )
    membership = ClinicMembership(
        clinic_id=clinic_id,
        user_id=worker.id,
        role="worker",
    )
    bound: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "app.api.routes.notifications.set_rls_clinic",
        lambda *_args: bound.append(("clinic", *_args[1:])),
    )
    monkeypatch.setattr(
        "app.api.routes.notifications.set_rls_actor",
        lambda *_args, **kwargs: bound.append(("actor", *_args[1:], kwargs["role"])),
    )
    _bind_callback_worker(
        _BindSession("sqlite", (membership, worker)),  # type: ignore[arg-type]
        clinic_id,
    )
    assert bound == [("clinic", clinic_id), ("actor", worker.id, "worker")]


def test_notification_lookup_and_list_role_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    patient_id = uuid.uuid4()
    visit_id = uuid.uuid4()
    staff = _context(clinic_id=clinic_id)
    patient = _context(
        clinic_id=clinic_id,
        role="patient",
        linked_patient_id=patient_id,
    )
    with pytest.raises(HTTPException) as missing:
        _notification(
            _Session([None]),  # type: ignore[arg-type]
            staff,
            uuid.uuid4(),
        )
    assert missing.value.status_code == 404
    other_patient_notification = _notification_model(
        clinic_id=clinic_id,
        patient_id=uuid.uuid4(),
    )
    with pytest.raises(HTTPException) as hidden:
        _notification(
            _Session([other_patient_notification]),  # type: ignore[arg-type]
            patient,
            other_patient_notification.id,
        )
    assert hidden.value.status_code == 404

    monkeypatch.setattr("app.api.routes.notifications.get_patient", lambda *_args: None)
    with pytest.raises(HTTPException) as forbidden:
        list_appointment_notifications(
            patient_id,
            visit_id,
            _Session(),  # type: ignore[arg-type]
            _context(clinic_id=clinic_id, role="admin"),
        )
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as no_visit:
        list_appointment_notifications(
            patient_id,
            visit_id,
            _Session([None]),  # type: ignore[arg-type]
            staff,
        )
    assert no_visit.value.status_code == 404

    visit = PatientVisit(
        id=visit_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        visit_type="follow_up",
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
    )
    notification = _notification_model(
        clinic_id=clinic_id,
        patient_id=patient_id,
        visit_id=visit_id,
    )
    listed = list_appointment_notifications(
        patient_id,
        visit_id,
        _Session([visit, [notification], [], []]),  # type: ignore[arg-type]
        staff,
    )
    assert [item.id for item in listed] == [notification.id]


def test_create_resend_revoke_and_acknowledge_reject_invalid_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    patient_id = uuid.uuid4()
    visit_id = uuid.uuid4()
    staff = _context(clinic_id=clinic_id)
    monkeypatch.setattr("app.api.routes.notifications.get_patient", lambda *_args: None)
    with pytest.raises(HTTPException) as patient_delivery:
        create_appointment_notification(
            patient_id,
            visit_id,
            AppointmentDeliveryCreate(channel="sms", destination="+6591234567"),
            _Session(),  # type: ignore[arg-type]
            _context(
                clinic_id=clinic_id,
                role="patient",
                linked_patient_id=patient_id,
            ),
        )
    assert patient_delivery.value.status_code == 403
    with pytest.raises(HTTPException) as no_visit:
        create_appointment_notification(
            patient_id,
            visit_id,
            AppointmentDeliveryCreate(channel="sms", destination="+6591234567"),
            _Session([None]),  # type: ignore[arg-type]
            staff,
        )
    assert no_visit.value.status_code == 404
    visit = PatientVisit(
        id=visit_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        visit_type="follow_up",
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
    )
    with pytest.raises(HTTPException) as past:
        create_appointment_notification(
            patient_id,
            visit_id,
            AppointmentDeliveryCreate(
                channel="sms",
                destination="+6591234567",
                scheduled_for=datetime.now(UTC) - timedelta(minutes=1),
            ),
            _Session([visit]),  # type: ignore[arg-type]
            staff,
        )
    assert past.value.status_code == 422
    monkeypatch.setattr(
        "app.api.routes.notifications.queue_notification",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            NotificationChannelUnavailable("NOTIFICATION_CHANNEL_UNAVAILABLE")
        ),
    )
    with pytest.raises(HTTPException) as channel_unavailable:
        create_appointment_notification(
            patient_id,
            visit_id,
            AppointmentDeliveryCreate(channel="sms", destination="+6591234567"),
            _Session([visit]),  # type: ignore[arg-type]
            staff,
        )
    assert channel_unavailable.value.status_code == 503

    queued_for_persistence = _notification_model(
        clinic_id=clinic_id,
        patient_id=patient_id,
        visit_id=visit_id,
    )

    class _MissingPersistedSession(_Session):
        def get(self, _model: object, _identifier: object) -> None:
            return None

    monkeypatch.setattr(
        "app.api.routes.notifications.queue_notification",
        lambda *_args, **_kwargs: (queued_for_persistence, False),
    )
    monkeypatch.setattr(
        "app.api.routes.notifications._rebind_delivery_actor", lambda *_args: None
    )
    with pytest.raises(HTTPException) as missing_persisted:
        create_appointment_notification(
            patient_id,
            visit_id,
            AppointmentDeliveryCreate(
                channel="sms",
                destination="+6591234567",
                scheduled_for=datetime.now(UTC) + timedelta(hours=1),
            ),
            _MissingPersistedSession([visit]),  # type: ignore[arg-type]
            staff,
        )
    assert missing_persisted.value.status_code == 409

    submitted = _notification_model(clinic_id=clinic_id, state="submitted")
    with pytest.raises(HTTPException) as resend_submitted:
        resend_notification(
            submitted.id,
            _Session([submitted, submitted]),  # type: ignore[arg-type]
            staff,
        )
    assert resend_submitted.value.status_code == 409
    failed = _notification_model(clinic_id=clinic_id, state="failed")
    monkeypatch.setattr(
        "app.api.routes.notifications.validate_notification_destination",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            NotificationChannelUnavailable("NOTIFICATION_PHONE_INVALID")
        ),
    )
    with pytest.raises(HTTPException) as invalid_resend:
        resend_notification(
            failed.id,
            _Session([failed, failed]),  # type: ignore[arg-type]
            staff,
        )
    assert invalid_resend.value.status_code == 422
    failed_with_destination = _notification_model(clinic_id=clinic_id, state="failed")
    with pytest.raises(HTTPException) as invalid_replacement:
        resend_notification(
            failed_with_destination.id,
            _Session([failed_with_destination, failed_with_destination]),  # type: ignore[arg-type]
            staff,
            NotificationResendRequest(channel="sms", destination="+6590000000"),
        )
    assert invalid_replacement.value.status_code == 422

    delivered = _notification_model(clinic_id=clinic_id, state="delivered")
    with pytest.raises(HTTPException) as revoke_delivered:
        revoke_notification(
            delivered.id,
            _Session([delivered, delivered]),  # type: ignore[arg-type]
            staff,
        )
    assert revoke_delivered.value.status_code == 409
    with pytest.raises(HTTPException) as admin_ack:
        acknowledge_notification(
            submitted.id,
            _Session([submitted]),  # type: ignore[arg-type]
            _context(clinic_id=clinic_id, role="admin"),
        )
    assert admin_ack.value.status_code == 403
    queued = _notification_model(clinic_id=clinic_id, state="queued")
    with pytest.raises(HTTPException) as queued_ack:
        acknowledge_notification(
            queued.id,
            _Session([queued, queued]),  # type: ignore[arg-type]
            staff,
        )
    assert queued_ack.value.status_code == 409

    readable = _notification_model(clinic_id=clinic_id, state="submitted")
    public = read_notification(
        readable.id,
        _Session([readable, [], []]),  # type: ignore[arg-type]
        staff,
    )
    assert public.id == readable.id


def _callback_body(notification: NotificationOutbox) -> ProviderCallbackCreate:
    return ProviderCallbackCreate(
        notification_id=notification.id,
        provider_event_id="event-fixture",
        provider_message_id="message-fixture",
        event_type="delivered",
        occurred_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )


def _callback_signature(
    notification: NotificationOutbox, provider: str, body: ProviderCallbackCreate
) -> str:
    return receipt_signature(
        {
            "notification_id": str(notification.id),
            "provider": provider,
            "provider_event_id": body.provider_event_id,
            "provider_message_id": body.provider_message_id,
            "event_type": body.event_type,
            "occurred_at": canonical_receipt_timestamp(body.occurred_at),
        }
    )


def test_generic_callback_hides_unknown_notifications_and_maps_internal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notification = _notification_model()
    body = _callback_body(notification)
    signature = _callback_signature(notification, "deterministic-sms", body)
    monkeypatch.setattr(
        "app.api.routes.notifications._bind_callback_worker", lambda *_args: None
    )
    with pytest.raises(HTTPException) as unknown:
        provider_callback(
            notification.clinic_id,
            "deterministic-sms",
            body,
            _Session([None]),  # type: ignore[arg-type]
            signature,
        )
    assert unknown.value.status_code == 404
    monkeypatch.setattr(
        "app.api.routes.notifications.apply_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unexpected")),
    )
    with pytest.raises(HTTPException) as invalid:
        provider_callback(
            notification.clinic_id,
            "deterministic-sms",
            body,
            _Session([notification]),  # type: ignore[arg-type]
            signature,
        )
    assert invalid.value.status_code == 403


class _Form:
    def __init__(self, params: dict[str, str]) -> None:
        self.params = params

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self.params.items())


class _Request:
    def __init__(self, params: dict[str, str]) -> None:
        self.params = params

    async def form(self) -> _Form:
        return _Form(self.params)


class _AcceptingTwilioValidator:
    def __init__(self, _token: str) -> None:
        return None

    def validate(self, _url: str, _params: dict[str, str], _signature: str) -> bool:
        return True


def test_twilio_callback_rejects_missing_envelope_bad_sid_and_attempt_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    notification = _notification_model(clinic_id=clinic_id)
    with pytest.raises(HTTPException) as missing_envelope:
        asyncio.run(
            twilio_provider_callback(
                clinic_id,
                notification.id,
                _Request({}),  # type: ignore[arg-type]
                _Session(),  # type: ignore[arg-type]
                None,
            )
        )
    assert missing_envelope.value.status_code == 403

    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "fixture-token")
    monkeypatch.setattr(
        "app.api.routes.notifications.twilio_status_callback_url",
        lambda **_kwargs: "https://notifications.example/twilio",
    )
    monkeypatch.setattr(
        "app.api.routes.notifications.RequestValidator", _AcceptingTwilioValidator
    )
    with pytest.raises(HTTPException) as bad_sid:
        asyncio.run(
            twilio_provider_callback(
                clinic_id,
                notification.id,
                _Request({"MessageSid": "!", "MessageStatus": "delivered"}),  # type: ignore[arg-type]
                _Session(),  # type: ignore[arg-type]
                "signed",
            )
        )
    assert bad_sid.value.status_code == 422

    monkeypatch.setattr(
        "app.api.routes.notifications._bind_callback_worker", lambda *_args: None
    )
    valid_form = {"MessageSid": "SM_fixture", "MessageStatus": "delivered"}
    with pytest.raises(HTTPException) as unknown:
        asyncio.run(
            twilio_provider_callback(
                clinic_id,
                notification.id,
                _Request(valid_form),  # type: ignore[arg-type]
                _Session([None]),  # type: ignore[arg-type]
                "signed",
            )
        )
    assert unknown.value.status_code == 404
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(
            twilio_provider_callback(
                clinic_id,
                notification.id,
                _Request(valid_form),  # type: ignore[arg-type]
                _Session([notification, None]),  # type: ignore[arg-type]
                "signed",
            )
        )
    assert mismatch.value.status_code == 409


def test_twilio_callback_maps_receipt_conflict_and_persists_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    notification = _notification_model(clinic_id=clinic_id, state="submitted")
    attempt = NotificationAttempt(
        clinic_id=clinic_id,
        notification_id=notification.id,
        attempt_no=1,
        provider="twilio-sms",
        provider_message_id="SM_fixture",
        request_sha256="a" * 64,
        status="submitted",
    )
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "fixture-token")
    monkeypatch.setattr(
        "app.api.routes.notifications.twilio_status_callback_url",
        lambda **_kwargs: "https://notifications.example/twilio",
    )
    monkeypatch.setattr(
        "app.api.routes.notifications.RequestValidator", _AcceptingTwilioValidator
    )
    monkeypatch.setattr(
        "app.api.routes.notifications._bind_callback_worker", lambda *_args: None
    )
    form = {"MessageSid": "SM_fixture", "MessageStatus": "delivered"}
    monkeypatch.setattr(
        "app.api.routes.notifications.apply_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("NOTIFICATION_RECEIPT_EVENT_CONFLICT")
        ),
    )
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(
            twilio_provider_callback(
                clinic_id,
                notification.id,
                _Request(form),  # type: ignore[arg-type]
                _Session([notification, attempt, None]),  # type: ignore[arg-type]
                "signed",
            )
        )
    assert conflict.value.status_code == 409

    monkeypatch.setattr(
        "app.api.routes.notifications.apply_receipt",
        lambda *_args, **_kwargs: notification,
    )
    monkeypatch.setattr(
        "app.api.routes.notifications._public",
        lambda _session, value: value,
    )
    failed_form = {
        "MessageSid": "SM_fixture",
        "MessageStatus": "failed",
        "ErrorCode": "30003",
    }
    session = _Session([notification, attempt, None])
    updated = asyncio.run(
        twilio_provider_callback(
            clinic_id,
            notification.id,
            _Request(failed_form),  # type: ignore[arg-type]
            session,  # type: ignore[arg-type]
            "signed",
        )
    )
    assert updated is notification
    assert attempt.error_class == "provider"
    assert attempt.error_code == "TWILIO_30003"
    assert attempt in session.added
    assert session.commits == 1
