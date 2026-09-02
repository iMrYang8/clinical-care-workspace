from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    AIRun,
    Job,
    JobAttempt,
    PatientGlanceSnapshot,
    PatientPublication,
    ProviderCircuitState,
)
from app.services.ai_jobs import process_job, worker_context_for_job
from app.services.egress import QualifiedRedactedText
from app.services.provider_resilience import retry_delay_seconds
from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
)


class _FakeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def move_to(self, value: datetime) -> None:
        assert value.tzinfo is not None
        self.current = value


def _retry_delay(history_item: dict[str, Any]) -> int:
    attempted_at = datetime.fromisoformat(str(history_item["attempted_at"]))
    next_retry_at = datetime.fromisoformat(str(history_item["next_retry_at"]))
    return round((next_retry_at - attempted_at).total_seconds())


def test_one_hour_503_outage_persists_recovery_and_review_only_glance(
    client: TestClient,
    auth_headers,
    owner_session: Session,
    monkeypatch,
) -> None:
    """Exercise every retry slot against PostgreSQL and the public API."""

    import app.api.routes.patients as patient_routes
    import app.services.ai_jobs as ai_jobs
    import app.services.nightingale as nightingale

    started_at = datetime.now(UTC).replace(microsecond=0)
    clock = _FakeClock(started_at)
    provider_calls: list[datetime] = []

    class _ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            if tz is None:
                return clock.now().replace(tzinfo=None)
            return clock.now().astimezone(tz)

    class HourLong503Provider:
        review_model = None

        async def extract(
            self, payload: QualifiedRedactedText, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            del context
            redacted_text = payload.text
            provider_calls.append(clock.now())
            if clock.now() < started_at + timedelta(hours=1):
                request = httpx.Request("POST", "https://provider.invalid/v1/text")
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError(
                    "synthetic provider outage",
                    request=request,
                    response=response,
                )
            quote = "follow up in one week"
            start = redacted_text.index(quote)
            return ClinicalNoteDraft(
                summary="Provider recovered; clinician review remains available.",
                facts=[
                    ClinicalFact(
                        fact_type="follow_up",
                        value=quote,
                        evidence_start=start,
                        evidence_end=start + len(quote),
                        evidence_quote=quote,
                        feature_keys=["topic:follow_up"],
                    )
                ],
                provider="synthetic-remote",
                model="outage-recovery-model",
            )

    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    monkeypatch.setattr(ai_jobs, "get_datetime_utc", clock.now)
    monkeypatch.setattr(nightingale, "get_datetime_utc", clock.now)
    monkeypatch.setattr(patient_routes, "datetime", _ClockDateTime)
    monkeypatch.setattr(ai_jobs, "redaction_is_qualified", lambda *_args, **_kw: True)
    monkeypatch.setattr(
        ai_jobs, "_configured_remote_provider", lambda *_args: HourLong503Provider()
    )

    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    content = "Please follow up in one week."
    source_response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Outage acceptance source",
            "content": content,
        },
    )
    assert source_response.status_code == 201, source_response.text
    source = source_response.json()
    quote = "follow up in one week"
    quote_start = content.index(quote)
    stored_priority = client.post(
        f"/api/v1/entries/{source['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": source["version_id"],
            "start_offset": quote_start,
            "end_offset": quote_start + len(quote),
            "exact_quote": quote,
            "prefix": content[:quote_start],
            "suffix": content[quote_start + len(quote) :],
            "label": "Existing clinician-confirmed follow-up",
            "clinician_confirmed": True,
        },
    )
    assert stored_priority.status_code == 201, stored_priority.text
    accepted_priority = client.post(
        f"/api/v1/highlights/{stored_priority.json()['id']}/accept",
        headers=headers | {"Idempotency-Key": "accept-stored-outage-priority"},
    )
    assert accepted_priority.status_code == 200, accepted_priority.text
    assert accepted_priority.json()["status"] == "accepted"

    submitted = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "one-hour-503-outage"},
        json={"source_entry_version_id": source["version_id"]},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["visible_state"] == "queued"
    source_job_id = uuid.UUID(submitted.json()["id"])

    def run_durable_job(job_id: uuid.UUID) -> Job:
        owner_session.expire_all()
        job = owner_session.get(Job, job_id)
        assert job is not None
        context = worker_context_for_job(owner_session, job)
        assert context is not None
        processed = asyncio.run(process_job(owner_session, context, job.id))
        owner_session.expire_all()
        return processed

    run_durable_job(source_job_id)
    owner_session.expire_all()
    source_job = owner_session.get(Job, source_job_id)
    assert source_job is not None
    assert source_job.state == "needs_review"
    assert source_job.error_code == "PROVIDER_HTTP_503"
    assert source_job.error_class == "transient"
    assert source_job.provider_outage is True
    assert source_job.next_run_at is not None
    assert source_job.retry_history_json[-1]["error_code"] == "PROVIDER_HTTP_503"
    recovery_job_id = uuid.UUID(
        str(source_job.retry_history_json[-1]["recovery_job_id"])
    )

    first_status = client.get(f"/api/v1/jobs/{source_job_id}", headers=headers)
    assert first_status.status_code == 200, first_status.text
    assert first_status.json()["visible_state"] == "delayed"
    assert first_status.json()["provider_outage"] is True
    assert first_status.json()["retry_after_seconds"] == _retry_delay(
        source_job.retry_history_json[-1]
    )

    owner_session.expire_all()
    recovery_job = owner_session.get(Job, recovery_job_id)
    assert recovery_job is not None
    # Four failed recovery probes persist the 2m, 10m, 30m, and 60m slots.
    # The fifth probe runs only after the simulated one-hour outage is over.
    for _ in range(4):
        assert recovery_job.next_run_at is not None
        clock.move_to(recovery_job.next_run_at)
        run_durable_job(recovery_job_id)
        owner_session.expire_all()
        recovery_job = owner_session.get(Job, recovery_job_id)
        assert recovery_job is not None
        assert recovery_job.state == "failed"
        assert recovery_job.error_code == "PROVIDER_HTTP_503"
        assert recovery_job.error_class == "transient"

    history = recovery_job.retry_history_json
    all_delays = [_retry_delay(source_job.retry_history_json[-1])] + [
        _retry_delay(item) for item in history[1:]
    ]
    assert len(all_delays) == 5
    bounds = [(24, 36), (96, 144), (480, 720), (1_440, 2_160), (2_880, 4_320)]
    assert all(
        lower <= delay <= upper
        for delay, (lower, upper) in zip(all_delays, bounds, strict=True)
    )
    assert all_delays == [
        retry_delay_seconds(source_job_id, 1),
        *[retry_delay_seconds(recovery_job_id, index) for index in range(2, 6)],
    ]

    owner_session.expire_all()
    attempts = owner_session.exec(
        select(JobAttempt)
        .where(JobAttempt.job_id.in_([source_job_id, recovery_job_id]))
        .order_by(JobAttempt.started_at, JobAttempt.attempt_no)
    ).all()
    assert len(attempts) == 5
    assert attempts[0].status == "completed"
    assert all(attempt.error_code == "PROVIDER_HTTP_503" for attempt in attempts)
    assert all(attempt.error_class == "transient" for attempt in attempts)
    assert all(attempt.retry_scheduled_at is not None for attempt in attempts)

    circuit = owner_session.exec(select(ProviderCircuitState)).one()
    assert circuit.state == "open"
    assert circuit.consecutive_failures == 5
    assert circuit.last_error_class == "transient"
    assert circuit.opened_at == started_at
    assert circuit.next_probe_at == recovery_job.next_run_at

    # At exactly one hour the provider remains isolated until its persisted,
    # jittered probe time; old priorities stay visible with their true age and
    # new rule output is confined to the uncapped review queue.
    clock.move_to(started_at + timedelta(hours=1))
    during_outage = client.get(f"/api/v1/patients/{patient_id}/glance", headers=headers)
    assert during_outage.status_code == 200, during_outage.text
    glance = during_outage.json()
    assert glance["freshness_state"] == "stale"
    assert glance["provider_outage"] is True
    assert glance["age_seconds"] == 3_600
    assert "stored priorities remain visible" in glance["outage_message"].lower()
    assert glance["fallback_kind"] == "stored"
    # Clinician-confirmed items are protected and therefore live in the
    # uncapped, disjoint clinical-review queue rather than consuming the
    # ordinary top-five priority cap. They still remain visible during outage.
    assert stored_priority.json()["id"] in {
        card["highlight_id"] for card in glance["review_cards"]
    }
    rule_suggestions = [
        card
        for card in glance["review_cards"]
        if card.get("fallback_kind") == "rule_derived"
    ]
    assert rule_suggestions
    assert all(card["review_state"] != "ready" for card in rule_suggestions)
    assert glance["safety_review_required"] is True
    assert owner_session.exec(select(PatientPublication)).all() == []

    delayed_recovery = client.get(f"/api/v1/jobs/{recovery_job_id}", headers=headers)
    assert delayed_recovery.status_code == 200, delayed_recovery.text
    assert delayed_recovery.json()["visible_state"] == "delayed"
    assert delayed_recovery.json()["provider_outage"] is True
    assert delayed_recovery.json()["outage_age_seconds"] == 3_600
    assert delayed_recovery.json()["retry_after_seconds"] > 0

    assert recovery_job.next_run_at is not None
    assert recovery_job.next_run_at > started_at + timedelta(hours=1)
    clock.move_to(recovery_job.next_run_at)
    recovered = run_durable_job(recovery_job_id)
    # Provider transport recovery closes the circuit, while the pre-existing
    # clinical assertion conflict still correctly keeps the output in review.
    assert recovered.state == "needs_review"
    assert len(provider_calls) == 6
    assert provider_calls[-1] >= started_at + timedelta(hours=1)

    owner_session.expire_all()
    recovered_job = owner_session.get(Job, recovery_job_id)
    assert recovered_job is not None
    assert recovered_job.error_code is None
    assert recovered_job.error_class is None
    assert recovered_job.provider_outage is False
    assert recovered_job.next_run_at is None
    recovered_circuit = owner_session.exec(select(ProviderCircuitState)).one()
    assert recovered_circuit.state == "closed"
    assert recovered_circuit.consecutive_failures == 0
    assert recovered_circuit.last_success_at == clock.now()
    recovered_attempts = owner_session.exec(
        select(JobAttempt)
        .where(JobAttempt.job_id == recovery_job_id)
        .order_by(JobAttempt.attempt_no)
    ).all()
    assert [attempt.status for attempt in recovered_attempts] == [
        "failed",
        "failed",
        "failed",
        "failed",
        "completed",
    ]
    fallback_run = owner_session.exec(
        select(AIRun).where(AIRun.job_id == source_job_id)
    ).one()
    assert fallback_run.status == "fallback"
    assert fallback_run.needs_review is True
    assert fallback_run.output_entry_id is not None
    assert owner_session.exec(select(PatientPublication)).all() == []

    owner_session.expire_all()
    snapshot = owner_session.exec(
        select(PatientGlanceSnapshot).where(
            PatientGlanceSnapshot.patient_id == uuid.UUID(patient_id)
        )
    ).one()
    assert snapshot.generated_at == clock.now()
    after_recovery = client.get(
        f"/api/v1/patients/{patient_id}/glance", headers=headers
    )
    assert after_recovery.status_code == 200, after_recovery.text
    assert after_recovery.json()["provider_outage"] is False
    assert after_recovery.json().get("outage_message") is None
    recovered_suggestions = [
        card
        for card in after_recovery.json()["review_cards"]
        if card["label"] == "follow up in one week"
    ]
    assert recovered_suggestions
    assert all(card.get("fallback_kind") is None for card in recovered_suggestions)
