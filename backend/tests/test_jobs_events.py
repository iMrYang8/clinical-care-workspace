import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.db import engine
from app.models import (
    AIRun,
    ClinicMembership,
    DomainEvent,
    EntryVersion,
    Job,
    JobAttempt,
    RedactionRun,
    User,
    get_datetime_utc,
)
from app.seed import demo_id
from app.services.ai_jobs import (
    _map_facts_to_source,
    canonical_request_hash,
    claim_job,
    worker_context_for_job,
)
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


def test_server_trusted_name_and_risk_force_redaction_and_second_review(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    import app.services.ai_jobs as ai_jobs
    from app.core.config import settings

    outbound: list[tuple[str, str]] = []

    class ReviewSpy:
        review_model = "configured-review-model"

        @staticmethod
        def _draft(redacted_text: str, model: str) -> ClinicalNoteDraft:
            quote = "critical allergy"
            start = redacted_text.index(quote)
            return ClinicalNoteDraft(
                summary="redacted synthetic summary",
                facts=[
                    ClinicalFact(
                        fact_type="allergy",
                        value=quote,
                        evidence_start=start,
                        evidence_end=start + len(quote),
                        evidence_quote=quote,
                        feature_keys=["entity:allergy"],
                        critical=True,
                    )
                ],
                provider="synthetic-remote",
                model=model,
            )

        async def extract(
            self, redacted_text: str, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            outbound.append(("primary", redacted_text))
            return self._draft(redacted_text, "configured-primary-model")

        async def review(
            self,
            redacted_text: str,
            context: ExtractionContext,
            primary: ClinicalNoteDraft,
        ) -> ClinicalNoteDraft:
            outbound.append(("review", redacted_text))
            return self._draft(redacted_text, self.review_model)

    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    monkeypatch.setattr(ai_jobs, "_configured_remote_provider", lambda: ReviewSpy())
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    source = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Trusted redaction source",
            "content": (
                "Alex Synthetic S1234567D MRN:AB-12345 91234567 has critical allergy."
            ),
        },
    ).json()
    response = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "server-risk-review"},
        json={
            "source_entry_version_id": source["version_id"],
            # The untrusted legacy risk fields are ignored by the request model.
            # No client-provided name dictionary is sent.
            "high_risk": False,
            "conflict_review": False,
        },
    )
    assert response.status_code == 200, response.text
    run = response.json()["ai_run"]
    assert response.json()["state"] == "completed"
    assert run["risk_tier"] == "high"
    assert run["model"] == "configured-primary-model"
    assert run["review_model"] == "configured-review-model"
    assert run["review_status"] == "consistent"
    assert [stage for stage, _ in outbound] == ["primary", "review"]
    for _, egress in outbound:
        assert "Alex Synthetic" not in egress
        assert "S1234567D" not in egress
        assert "AB-12345" not in egress
        assert "91234567" not in egress

    with Session(engine) as session:
        job = session.get(Job, uuid.UUID(response.json()["id"]))
        ai_run = session.exec(select(AIRun).where(AIRun.job_id == job.id)).one()
        attempt = session.exec(
            select(JobAttempt).where(JobAttempt.job_id == job.id)
        ).one()
        output_version = session.get(EntryVersion, ai_run.output_entry_version_id)
        worker_membership = session.get(ClinicMembership, demo_id("membership-worker"))
        clinician = session.get(User, demo_id("user-clinician"))
        worker = session.get(User, demo_id("user-worker"))
        assert job is not None
        assert output_version is not None
        assert worker_membership is not None
        assert clinician is not None and worker is not None
        assert job.created_by_id == clinician.id
        assert attempt.worker_membership_id == worker_membership.id
        assert ai_run.executed_by_worker_membership_id == worker_membership.id
        assert output_version.author_id == worker.id
        assert b"critical allergy" not in ai_run.primary_output_ciphertext
        assert ai_run.review_output_ciphertext is not None
        assert b"critical allergy" not in ai_run.review_output_ciphertext


def test_concurrent_idempotency_and_skip_locked_worker_lease(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    source = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Queued remote source",
            "content": "Synthetic follow up note.",
        },
    ).json()
    barrier = Barrier(2)

    def submit() -> tuple[int, dict]:
        barrier.wait()
        response = client.post(
            f"/api/v1/patients/{patient_id}/ai/ingest",
            headers=headers | {"Idempotency-Key": "concurrent-job-key"},
            json={"source_entry_version_id": source["version_id"]},
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result() for future in [pool.submit(submit), pool.submit(submit)]
        ]
    assert [status for status, _ in results] == [200, 200]
    assert len({body["id"] for _, body in results}) == 1
    assert all(body["state"] == "pending" for _, body in results)
    assert all(body["attempt_count"] == 0 for _, body in results)

    job_id = uuid.UUID(results[0][1]["id"])
    with Session(engine) as first, Session(engine) as second:
        first_job = first.get(Job, job_id)
        second_job = second.get(Job, job_id)
        assert first_job is not None and second_job is not None
        first_context = worker_context_for_job(first, first_job)
        second_context = worker_context_for_job(second, second_job)
        assert first_context is not None and second_context is not None
        claimed = claim_job(first, first_context, job_id)
        assert claimed.state == "running"
        assert claimed.locked_by == str(first_context.membership.id)
        with pytest.raises(HTTPException) as busy:
            claim_job(second, second_context, job_id)
        assert busy.value.detail["code"] == "JOB_NOT_CLAIMABLE"
        first.rollback()

        claimed_after_release = claim_job(second, second_context, job_id)
        assert claimed_after_release.id == job_id
        claimed_after_release.attempt_count = 1
        claimed_after_release.locked_until = get_datetime_utc() - timedelta(seconds=1)
        second.add(claimed_after_release)
        second.add(
            JobAttempt(
                clinic_id=claimed_after_release.clinic_id,
                job_id=job_id,
                worker_membership_id=second_context.membership.id,
                attempt_no=1,
            )
        )
        second.commit()

    with Session(engine) as recovery:
        recoverable = recovery.get(Job, job_id)
        assert recoverable is not None
        recovery_context = worker_context_for_job(recovery, recoverable)
        assert recovery_context is not None
        reclaimed = claim_job(recovery, recovery_context, job_id)
        assert reclaimed.state == "running"
        recovery.commit()
        expired = recovery.exec(
            select(JobAttempt).where(JobAttempt.job_id == job_id)
        ).one()
        assert expired.status == "failed"
        assert expired.error_code == "WORKER_LEASE_EXPIRED"
