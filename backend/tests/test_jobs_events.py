import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.db import engine
from app.models import AIRun, DomainEvent, JobAttempt, RedactionRun
from app.services.ai_jobs import _map_facts_to_source, canonical_request_hash
from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
)


@pytest.mark.unit
def test_request_hash_is_canonical_and_content_sensitive() -> None:
    patient_id = uuid.uuid4()
    left = canonical_request_hash(patient_id, "ai_ingest", {"b": 2, "a": 1})
    right = canonical_request_hash(patient_id, "ai_ingest", {"a": 1, "b": 2})
    changed = canonical_request_hash(patient_id, "ai_ingest", {"a": 1, "b": 3})
    assert left == right
    assert changed != left


@pytest.mark.unit
def test_ambiguous_or_missing_raw_evidence_is_discarded() -> None:
    facts = [
        ClinicalFact(
            fact_type="allergy",
            value="penicillin",
            evidence_start=0,
            evidence_end=8,
            evidence_quote="allergy",
            feature_keys=["entity:allergy"],
        ),
        ClinicalFact(
            fact_type="follow_up",
            value="soon",
            evidence_start=0,
            evidence_end=9,
            evidence_quote="follow up",
            feature_keys=["topic:follow_up"],
        ),
    ]
    mapped, failed = _map_facts_to_source(
        facts, "allergy appears twice: allergy. Please follow up."
    )
    assert failed is True
    assert [item.fact_type for item in mapped] == ["follow_up"]
    assert mapped[0].evidence_quote == "follow up"


def test_ai_job_idempotency_attempts_fallback_and_domain_event(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "PRESIDIO_NLP_MODEL", "nightingale_missing_model")
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    source = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Synthetic source",
            "content": "Tan Mei Ling S1234567D phone 91234567 reports allergy to penicillin.",
        },
    )
    assert source.status_code == 201, source.text
    payload = {
        "source_entry_version_id": source.json()["version_id"],
        "known_names": ["Tan Mei Ling"],
        "interaction_type": "doctor_consult",
    }

    created = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "synthetic-ingest-1"},
        json=payload,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["attempt_count"] == 1
    assert body["state"] == "needs_review"
    assert body["ai_run"]["status"] == "fallback"
    assert body["ai_run"]["fallback_reason"] in {
        "PRESIDIO_UNAVAILABLE",
        "DETERMINISTIC_MODE",
    }
    assert body["ai_run"]["output_entry_id"]
    assert "Tan Mei Ling" not in created.text
    preserved = client.get(f"/api/v1/entries/{source.json()['id']}", headers=headers)
    assert preserved.status_code == 200
    assert preserved.json()["content"].startswith("Tan Mei Ling")

    replay = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "synthetic-ingest-1"},
        json=payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == body["id"]
    assert replay.json()["attempt_count"] == 1

    reused = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "synthetic-ingest-1"},
        json=payload | {"interaction_type": "different"},
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    status = client.get(f"/api/v1/jobs/{body['id']}", headers=headers)
    assert status.status_code == 200
    assert status.json()["attempt_count"] == 1
    other_clinic = client.get(
        f"/api/v1/jobs/{body['id']}", headers=auth_headers("other_staff")
    )
    assert other_clinic.status_code == 404

    with Session(engine) as session:
        attempt = session.exec(select(JobAttempt)).one()
        run = session.exec(select(AIRun)).one()
        redaction = session.exec(select(RedactionRun)).one()
        events = session.exec(
            select(DomainEvent).where(DomainEvent.event_type == "ai.completed")
        ).all()
        assert attempt.status == "completed"
        assert run.job_id == attempt.job_id
        assert redaction.status == "fallback"
        assert redaction.residual_scan_passed is False
        assert events
        assert all("Tan Mei Ling" not in str(event.payload_json) for event in events)


def test_failed_job_retry_persists_each_attempt(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    import app.services.ai_jobs as ai_jobs
    from app.core.config import settings

    class RaisingProvider:
        async def extract(
            self, redacted_text: str, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            raise RuntimeError("synthetic provider outage")

    class WorkingProvider:
        async def extract(
            self, redacted_text: str, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            start = redacted_text.index("allergy")
            return ClinicalNoteDraft(
                summary="reviewed",
                facts=[
                    ClinicalFact(
                        fact_type="allergy",
                        value="allergy",
                        evidence_start=start,
                        evidence_end=start + 7,
                        evidence_quote="allergy",
                        feature_keys=["entity:allergy"],
                    )
                ],
                provider="synthetic-remote",
                model="configured-test-model",
            )

    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    monkeypatch.setattr(
        ai_jobs, "_configured_remote_provider", lambda: RaisingProvider()
    )
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Retry source",
            "content": "single allergy evidence",
        },
    ).json()
    failed = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "retry-fixture"},
        json={"source_entry_version_id": entry["version_id"]},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    assert failed.json()["attempt_count"] == 1

    monkeypatch.setattr(
        ai_jobs, "_configured_remote_provider", lambda: WorkingProvider()
    )
    retried = client.post(f"/api/v1/jobs/{failed.json()['id']}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "completed"
    assert retried.json()["attempt_count"] == 2
    with Session(engine) as session:
        attempts = session.exec(
            select(JobAttempt).where(
                JobAttempt.job_id == uuid.UUID(failed.json()["id"])
            )
        ).all()
        assert [attempt.status for attempt in attempts] == ["failed", "completed"]
