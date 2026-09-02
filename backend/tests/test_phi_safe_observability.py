import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session

import app.main as main_module
from app.api.deps import RequestContext
from app.api.routes.patient_access import (
    PatientAccessRecoveryCreate,
    PatientAccessRevokeRequest,
)
from app.api.routes.platform import PlatformContext, _audit
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.main import SafeAccessLogMiddleware
from app.models import (
    AuditEvent,
    ClinicMembership,
    DomainEvent,
    PlatformAdministrator,
    PlatformAuditEvent,
    ProvisionalSafetyAlertReviewRequest,
    User,
)
from app.services.nightingale import emit_change
from app.services.operational_events import (
    initialize_operational_event_store,
    list_operational_events,
    record_operational_event,
    run_operational_event_purge_loop,
)

PHI_CANARY = "S1234567D_SYNTHETIC_CLINICAL_CANARY"


class _CaptureSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


@pytest.mark.unit
def test_access_log_drops_url_query_body_headers_method_and_exception_text(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "operational-events.sqlite3"
    monkeypatch.setattr(settings, "OPERATIONAL_EVENT_DB_PATH", str(store_path))
    fixture = FastAPI()

    @fixture.post("/canary/{record_id}", name="phi_canary_probe")
    async def canary_probe(record_id: str, request: Request) -> None:
        del record_id
        await request.body()
        request.headers.get("X-PHI-Canary")
        raise RuntimeError(PHI_CANARY)

    fixture.add_middleware(SafeAccessLogMiddleware)
    with TestClient(fixture, raise_server_exceptions=False) as client:
        with caplog.at_level(logging.INFO, logger="nightingale.access"):
            failed = client.post(
                f"/canary/{PHI_CANARY}",
                params={"search": PHI_CANARY},
                headers={
                    "X-PHI-Canary": PHI_CANARY,
                    "X-Request-ID": PHI_CANARY,
                    "Authorization": f"Bearer {PHI_CANARY}",
                },
                json={"clinical_note": PHI_CANARY},
            )
            custom_method = client.request(
                PHI_CANARY,
                f"/canary/{PHI_CANARY}",
                params={"search": PHI_CANARY},
                headers={"X-PHI-Canary": PHI_CANARY},
                content=PHI_CANARY,
            )

    assert failed.status_code == 500
    assert failed.json() == {"detail": {"code": "INTERNAL_ERROR"}}
    assert custom_method.status_code in {404, 405}
    rendered = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "nightingale.access"
    )
    assert PHI_CANARY not in rendered
    assert "route=phi_canary_probe method=POST status=500" in rendered
    assert "method=OTHER" in rendered
    events = list_operational_events()
    assert [(item.route, item.method, item.status) for item in events] == [
        ("phi_canary_probe", "POST", 500),
        ("phi_canary_probe", "OTHER", custom_method.status_code),
    ]
    assert PHI_CANARY.encode() not in store_path.read_bytes()


def test_request_validation_error_drops_rejected_values_and_free_form_messages(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "validation-events.sqlite3"
    monkeypatch.setattr(settings, "OPERATIONAL_EVENT_DB_PATH", str(store_path))
    with caplog.at_level(logging.INFO, logger="nightingale.access"):
        response = client.post(
            "/api/v1/patient-access/login/start",
            json={"portal_id": PHI_CANARY, PHI_CANARY: PHI_CANARY},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "REQUEST_VALIDATION_FAILED"}}
    rendered = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "nightingale.access"
    )
    assert PHI_CANARY not in rendered
    assert PHI_CANARY.encode() not in store_path.read_bytes()
    event = list_operational_events()[-1]
    assert (event.route, event.method, event.status) == (
        "begin_patient_login",
        "POST",
        422,
    )


@pytest.mark.unit
def test_operational_event_repository_enforces_30_day_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        settings,
        "OPERATIONAL_EVENT_DB_PATH",
        str(tmp_path / "retention-events.sqlite3"),
    )
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    record_operational_event(
        request_id=str(uuid.uuid4()),
        route="old_request",
        method="GET",
        status=200,
        duration_ms=4,
        occurred_at=now - timedelta(days=31),
    )
    record_operational_event(
        request_id=str(uuid.uuid4()),
        route="current_request",
        method="POST",
        status=201,
        duration_ms=7,
        occurred_at=now,
    )

    events = list_operational_events()
    assert len(events) == 1
    assert events[0].route == "current_request"
    assert events[0].occurred_at == now
    assert events[0].expires_at == now + timedelta(days=30)


@pytest.mark.unit
def test_operational_event_schema_migrates_legacy_rows_to_addressable_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = tmp_path / "legacy-retention-events.sqlite3"
    monkeypatch.setattr(settings, "OPERATIONAL_EVENT_DB_PATH", str(store_path))
    occurred_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    with sqlite3.connect(store_path) as connection:
        connection.execute(
            """
            CREATE TABLE operational_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              occurred_at TEXT NOT NULL,
              request_id TEXT NOT NULL,
              route TEXT NOT NULL,
              method TEXT NOT NULL,
              status INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO operational_events (
              occurred_at, request_id, route, method, status, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                occurred_at.isoformat(),
                str(uuid.uuid4()),
                "legacy_request",
                "GET",
                200,
                3,
            ),
        )

    initialize_operational_event_store()

    migrated = list_operational_events()
    assert len(migrated) == 1
    assert migrated[0].occurred_at == occurred_at
    assert migrated[0].expires_at == occurred_at + timedelta(days=30)
    with sqlite3.connect(store_path) as connection:
        columns = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(operational_events)")
        }
        indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(operational_events)")
        }
    assert columns["expires_at"] == 1
    assert "ix_operational_events_expires_at" in indexes


@pytest.mark.unit
def test_periodic_purge_deletes_expired_event_during_quiet_deployment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        settings,
        "OPERATIONAL_EVENT_DB_PATH",
        str(tmp_path / "quiet-retention-events.sqlite3"),
    )
    initial_time = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    record_operational_event(
        request_id=str(uuid.uuid4()),
        route="only_request_before_quiet_period",
        method="GET",
        status=200,
        duration_ms=1,
        occurred_at=initial_time,
    )

    class FakeClock:
        now = initial_time
        sleep_calls = 0
        first_purge_completed = asyncio.Event()

        async def sleep(self, _seconds: float) -> None:
            self.sleep_calls += 1
            if self.sleep_calls == 1:
                self.now += timedelta(days=30, seconds=1)
                return
            self.first_purge_completed.set()
            await asyncio.Event().wait()

    async def exercise() -> None:
        clock = FakeClock()
        task = asyncio.create_task(
            run_operational_event_purge_loop(
                clock=lambda: clock.now,
                sleep=clock.sleep,
                interval_seconds=1,
            )
        )
        await clock.first_purge_completed.wait()
        assert list_operational_events() == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


@pytest.mark.unit
def test_application_lifespan_cancels_periodic_purge_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        settings,
        "OPERATIONAL_EVENT_DB_PATH",
        str(tmp_path / "lifespan-retention-events.sqlite3"),
    )

    async def exercise() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_for_shutdown() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(
            main_module, "run_operational_event_purge_loop", wait_for_shutdown
        )
        async with main_module.lifespan(FastAPI()):
            await started.wait()
            assert not cancelled.is_set()
        assert cancelled.is_set()

    asyncio.run(exercise())


@pytest.mark.unit
def test_operational_event_repository_tolerates_volume_chmod_and_normalizes_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_path = tmp_path / "volume" / "operational-events.sqlite3"
    monkeypatch.setattr(settings, "OPERATIONAL_EVENT_DB_PATH", str(store_path))

    def chmod_unavailable(_path: object, _mode: int) -> None:
        raise OSError("fixture volume does not expose chmod")

    monkeypatch.setattr("app.services.operational_events.os.chmod", chmod_unavailable)
    initialize_operational_event_store()
    record_operational_event(
        request_id="not-a-correlation-uuid",
        route="safe_fixture_route",
        method="GET",
        status=200,
        duration_ms=1,
        occurred_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )
    stored = list_operational_events()
    assert len(stored) == 1
    assert uuid.UUID(stored[0].request_id)
    assert stored[0].request_id != "not-a-correlation-uuid"

    with pytest.raises(ValueError, match="OPERATIONAL_EVENT_TIMESTAMP_NAIVE"):
        record_operational_event(
            request_id=str(uuid.uuid4()),
            route="safe_fixture_route",
            method="POST",
            status=201,
            duration_ms=2,
            occurred_at=datetime(2026, 9, 2, 12, 0),
        )


def test_patient_search_rejects_get_query_and_logs_neither_query_nor_post_body(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = auth_headers("staff") | {"X-PHI-Canary": PHI_CANARY}
    with caplog.at_level(logging.INFO, logger="nightingale.access"):
        legacy_get = client.get(
            "/api/v1/patients",
            params={"search": PHI_CANARY},
            headers=headers,
        )
        body_search = client.post(
            "/api/v1/patients/search",
            headers=headers,
            json={"search": PHI_CANARY, "limit": 50},
        )

    assert legacy_get.status_code == 422
    assert legacy_get.json()["detail"] == {
        "code": "SEARCH_BODY_REQUIRED",
        "method": "POST",
    }
    assert body_search.status_code == 200, body_search.text
    rendered = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "nightingale.access"
    )
    assert PHI_CANARY not in rendered
    assert "route=patients method=GET status=422" in rendered
    assert "route=search_patients method=POST status=200" in rendered


@pytest.mark.unit
def test_audit_free_text_is_encrypted_and_machine_metadata_is_allowlisted() -> None:
    clinic_id = uuid.uuid4()
    user = User(
        id=uuid.uuid4(),
        email="audit-fixture@nightingale.example",
        hashed_password="fixture-only",
        account_kind="staff",
    )
    membership = ClinicMembership(
        id=uuid.uuid4(),
        clinic_id=clinic_id,
        user_id=user.id,
        role="clinician",
    )
    capture = _CaptureSession()
    emit_change(
        cast(Session, capture),
        RequestContext(user=user, membership=membership),
        action="clinical.review_requested",
        resource_type="highlight",
        resource_id=uuid.uuid4(),
        reason_code="clinical_review_requested",
        clinical_rationale=PHI_CANARY,
        metadata={"reason": PHI_CANARY, "status": "review_required"},
    )

    audit = next(item for item in capture.added if isinstance(item, AuditEvent))
    event = next(item for item in capture.added if isinstance(item, DomainEvent))
    assert audit.reason_code == "clinical_review_requested"
    assert audit.metadata_json == {"status": "review_required"}
    assert PHI_CANARY not in str(audit.metadata_json)
    assert audit.clinical_rationale_ciphertext is not None
    assert (
        field_codec.decrypt_text(
            clinic_id,
            "audit_event.clinical_rationale",
            audit.id,
            audit.clinical_rationale_ciphertext,
        )
        == PHI_CANARY
    )
    assert PHI_CANARY not in str(event.payload_json)
    assert event.payload_json["reason_code"] == "clinical_review_requested"


@pytest.mark.unit
def test_platform_audit_replaces_free_form_request_id_with_opaque_uuid() -> None:
    user = User(
        id=uuid.uuid4(),
        email="platform-audit-fixture@nightingale.example",
        hashed_password="fixture-only",
        account_kind="staff",
    )
    administrator = PlatformAdministrator(id=uuid.uuid4(), user_id=user.id)
    capture = _CaptureSession()
    _audit(
        cast(Session, capture),
        PlatformContext(user=user, administrator=administrator),
        action="platform.synthetic_canary",
        request_id=PHI_CANARY,
        reason_code="synthetic_canary",
    )

    audit = next(item for item in capture.added if isinstance(item, PlatformAuditEvent))
    assert audit.request_id != PHI_CANARY
    assert str(uuid.UUID(audit.request_id)) == audit.request_id
    assert audit.reason_code == "synthetic_canary"
    assert audit.metadata_json == {}


@pytest.mark.unit
def test_caller_supplied_reason_codes_reject_free_text_phi() -> None:
    with pytest.raises(ValidationError):
        PatientAccessRevokeRequest(reason_code=PHI_CANARY)
    with pytest.raises(ValidationError):
        PatientAccessRecoveryCreate(
            phone="+6591234567",
            channel="sms",
            reason_code=PHI_CANARY,
        )
    with pytest.raises(ValidationError):
        ProvisionalSafetyAlertReviewRequest(reason_code=PHI_CANARY)

    # Internal callers also fail closed if an unexpected free-form value
    # reaches the central audit constructor.
    clinic_id = uuid.uuid4()
    user = User(
        id=uuid.uuid4(),
        email="invalid-reason-fixture@nightingale.example",
        hashed_password="fixture-only",
        account_kind="staff",
    )
    membership = ClinicMembership(
        id=uuid.uuid4(),
        clinic_id=clinic_id,
        user_id=user.id,
        role="staff",
    )
    capture = _CaptureSession()
    emit_change(
        cast(Session, capture),
        RequestContext(user=user, membership=membership),
        action="audit.invalid_reason_probe",
        resource_type="patient",
        resource_id=uuid.uuid4(),
        reason_code=PHI_CANARY,
    )
    audit = next(item for item in capture.added if isinstance(item, AuditEvent))
    assert audit.reason_code == "invalid_reason_code"
    assert PHI_CANARY not in str(audit.metadata_json)
