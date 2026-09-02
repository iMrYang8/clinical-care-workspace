import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from time import sleep

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
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
from app.services.egress import QualifiedRedactedText
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
        json=payload | {"interaction_type": "patient_insight"},
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
        attempt = session.exec(
            select(JobAttempt).where(JobAttempt.job_id == uuid.UUID(body["id"]))
        ).one()
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


def test_nonretryable_provider_failure_persists_review_only_fallback(
    client: TestClient, auth_headers, owner_session: Session, monkeypatch
) -> None:
    import app.services.ai_jobs as ai_jobs
    from app.core.config import settings

    class RaisingProvider:
        async def extract(
            self, payload: QualifiedRedactedText, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            del payload, context
            raise RuntimeError("S1234567D")

    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    monkeypatch.setattr(
        ai_jobs, "_configured_remote_provider", lambda *_: RaisingProvider()
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
    fallback = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "retry-fixture"},
        json={"source_entry_version_id": entry["version_id"]},
    )
    assert fallback.status_code == 200, fallback.text
    payload = fallback.json()
    assert payload["state"] == "needs_review"
    assert payload["attempt_count"] == 1
    assert payload["error_code"] == "PROVIDER_FAILURE"
    assert payload["error_class"] == "unknown"
    assert payload["provider_outage"] is False
    assert payload["next_run_at"] is None
    assert payload["ai_run"]["status"] == "fallback"
    assert payload["ai_run"]["fallback_reason"] == "PROVIDER_FAILURE"
    assert payload["ai_run"]["needs_review"] is True
    assert payload["retry_history"][-1]["next_retry_at"] is None
    assert "S1234567D" not in fallback.text

    # Review-only output is durable, but an unknown/permanent transport error
    # does not manufacture a retry schedule or re-run the provider implicitly.
    retried = client.post(f"/api/v1/jobs/{payload['id']}/retry", headers=headers)
    assert retried.status_code == 409, retried.text
    assert retried.json()["detail"]["code"] == "JOB_NOT_RETRYABLE"
    owner_session.expire_all()
    attempts = owner_session.exec(
        select(JobAttempt).where(JobAttempt.job_id == uuid.UUID(payload["id"]))
    ).all()
    assert [attempt.status for attempt in attempts] == ["completed"]
    assert attempts[0].error_code == "PROVIDER_FAILURE"
    assert attempts[0].retry_scheduled_at is None


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
            self, payload: QualifiedRedactedText, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            redacted_text = payload.text
            outbound.append(("primary", redacted_text))
            return self._draft(redacted_text, "configured-primary-model")

        async def review(
            self,
            payload: QualifiedRedactedText,
            context: ExtractionContext,
            primary: ClinicalNoteDraft,
        ) -> ClinicalNoteDraft:
            redacted_text = payload.text
            outbound.append(("review", redacted_text))
            return self._draft(redacted_text, self.review_model)

    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    monkeypatch.setattr(ai_jobs, "_configured_remote_provider", lambda *_: ReviewSpy())
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
                "Alex Tan S1234567D MRN:AB-12345 91234567 has critical allergy."
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
        assert "Alex Tan" not in egress
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
        assert claimed.locked_by is not None
        uuid.UUID(claimed.locked_by)
        with pytest.raises(HTTPException) as busy:
            claim_job(second, second_context, job_id)
        assert busy.value.detail["code"] == "JOB_NOT_CLAIMABLE"
        first.rollback()

        claimed_after_release = claim_job(second, second_context, job_id)
        assert claimed_after_release.id == job_id
        assert claimed_after_release.locked_by is not None
        claimed_after_release.attempt_count = 1
        claimed_after_release.locked_until = get_datetime_utc() - timedelta(seconds=1)
        second.add(claimed_after_release)
        second.add(
            JobAttempt(
                id=uuid.UUID(claimed_after_release.locked_by),
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


def test_invalid_interaction_type_never_reaches_provider_or_plaintext_storage(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    import app.services.ai_jobs as ai_jobs

    outbound: list[str] = []

    class SpyProvider:
        async def extract(
            self, payload: QualifiedRedactedText, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            redacted_text = payload.text
            outbound.append(redacted_text)
            raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        ai_jobs, "_configured_remote_provider", lambda *_: SpyProvider()
    )
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    source = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Taxonomy boundary",
            "content": "routine follow up",
        },
    ).json()

    response = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "invalid-interaction"},
        json={
            "source_entry_version_id": source["version_id"],
            "interaction_type": "S1234567D",
        },
    )
    assert response.status_code == 422
    assert outbound == []
    with Session(engine) as session:
        assert (
            session.exec(
                select(Job).where(col(Job.kind).in_(["ai_ingest", "ai_reanalyze"]))
            ).all()
            == []
        )


def test_provider_warning_and_exception_text_are_mapped_to_fixed_codes(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    import app.services.ai_jobs as ai_jobs
    from app.core.config import settings

    class WarningProvider:
        async def extract(
            self, payload: QualifiedRedactedText, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            del payload, context
            return ClinicalNoteDraft(
                summary="fixed summary",
                facts=[],
                provider="synthetic-remote",
                model="configured-test-model",
                warnings=["S1234567D", "TAN_MEI_LING"],
            )

    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    monkeypatch.setattr(
        ai_jobs, "_configured_remote_provider", lambda *_: WarningProvider()
    )
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    source = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Warning boundary",
            "content": "routine follow up",
        },
    ).json()
    response = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "provider-warning-boundary"},
        json={"source_entry_version_id": source["version_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ai_run"]["warnings"] == ["PROVIDER_REPORTED_WARNING"]
    assert "S1234567D" not in response.text
    assert "TAN_MEI_LING" not in response.text
    with Session(engine) as session:
        run = session.exec(select(AIRun)).one()
        events = session.exec(select(DomainEvent)).all()
        assert run.warnings_json == ["PROVIDER_REPORTED_WARNING"]
        assert all("S1234567D" not in str(event.payload_json) for event in events)


def test_worker_runner_drains_remote_queue_with_job_semantics(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    import app.ai_worker as ai_worker
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
            "title": "Queued worker fixture",
            "content": "routine follow up",
        },
    ).json()
    queued = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "worker-runner-queue"},
        json={"source_entry_version_id": source["version_id"]},
    )
    assert queued.status_code == 200
    assert queued.json()["state"] == "pending"

    async def unexpected_failure(*_args, **_kwargs) -> None:
        raise RuntimeError("untrusted worker exception")

    with monkeypatch.context() as isolated:
        isolated.setattr(ai_worker, "process_job", unexpected_failure)
        assert asyncio.run(ai_worker.run_once()) == 0

    monkeypatch.setattr(settings, "AI_PROVIDER", "deterministic")
    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    assert asyncio.run(ai_worker.run_once()) == 1
    status = client.get(f"/api/v1/jobs/{queued.json()['id']}", headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] in {"completed", "needs_review"}
    assert status.json()["attempt_count"] == 1


def test_retry_serializes_against_worker_claim(
    client: TestClient, auth_headers, monkeypatch, owner_session: Session
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
            "title": "Retry claim CAS fixture",
            "content": "routine follow up",
        },
    ).json()
    queued = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "retry-claim-cas"},
        json={"source_entry_version_id": source["version_id"]},
    ).json()
    job_id = uuid.UUID(queued["id"])
    failed = owner_session.get(Job, job_id)
    assert failed is not None
    failed.state = "failed"
    failed.error_code = "SYNTHETIC_FAILURE"
    # Model the provider-retry path explicitly. Failed jobs without a due
    # ``next_run_at`` are permanent/manual-review failures and must not be
    # claimed automatically.
    failed.next_run_at = get_datetime_utc() - timedelta(seconds=1)
    owner_session.add(failed)
    owner_session.commit()

    with Session(engine) as worker_session:
        job = worker_session.get(Job, job_id)
        assert job is not None
        worker_context = worker_context_for_job(worker_session, job)
        assert worker_context is not None
        token = uuid.uuid4()
        claimed = claim_job(worker_session, worker_context, job_id, claim_token=token)
        claimed.attempt_count += 1
        worker_session.add(claimed)
        worker_session.add(
            JobAttempt(
                id=token,
                clinic_id=claimed.clinic_id,
                job_id=claimed.id,
                worker_membership_id=worker_context.membership.id,
                attempt_no=claimed.attempt_count,
            )
        )
        worker_session.flush()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post, f"/api/v1/jobs/{job_id}/retry", headers=headers
            )
            sleep(0.15)
            assert pending.done() is False
            worker_session.commit()
            response = pending.result(timeout=5)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "JOB_NOT_RETRYABLE",
        "state": "running",
    }
    with Session(engine) as session:
        stored = session.get(Job, job_id)
        attempt = session.exec(
            select(JobAttempt).where(JobAttempt.job_id == job_id)
        ).one()
        assert stored is not None
        assert stored.state == "running"
        assert stored.locked_by == str(token)
        assert attempt.status == "started"


def test_worker_runner_skips_inactive_user_and_selects_healthy_worker(
    client: TestClient, auth_headers, monkeypatch, owner_session: Session
) -> None:
    import app.ai_worker as ai_worker
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
            "title": "Healthy worker selection",
            "content": "routine follow up",
        },
    ).json()
    queued = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "healthy-second-worker"},
        json={"source_entry_version_id": source["version_id"]},
    ).json()

    first_worker = owner_session.get(User, demo_id("user-worker"))
    job = owner_session.get(Job, uuid.UUID(queued["id"]))
    assert first_worker is not None and job is not None
    first_worker.is_active = False
    second_user_id = uuid.uuid4()
    second_membership_id = uuid.uuid4()
    owner_session.add(first_worker)
    second_user = User(
        id=second_user_id,
        email="second.worker@nightingale.example",
        full_name="Second Synthetic Worker",
        hashed_password=first_worker.hashed_password,
        account_kind="service",
    )
    owner_session.add(second_user)
    owner_session.flush()
    owner_session.add(
        ClinicMembership(
            id=second_membership_id,
            clinic_id=job.clinic_id,
            user_id=second_user_id,
            role="worker",
        )
    )
    owner_session.commit()

    selected: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []

    async def capture_worker(
        _session: Session, context: RequestContext, job_id: uuid.UUID
    ) -> None:
        selected.append((context.user_id, context.membership.id, job_id))

    monkeypatch.setattr(ai_worker, "process_job", capture_worker)
    assert asyncio.run(ai_worker.run_once()) == 1
    assert selected == [(second_user_id, second_membership_id, uuid.UUID(queued["id"]))]


def test_expired_final_attempt_is_terminalized_without_provider_reentry(
    client: TestClient, auth_headers, monkeypatch, owner_session: Session
) -> None:
    import app.ai_worker as ai_worker
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
            "title": "Final attempt recovery",
            "content": "routine follow up",
        },
    ).json()
    queued = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "final-attempt-expiry"},
        json={"source_entry_version_id": source["version_id"]},
    ).json()
    job_id = uuid.UUID(queued["id"])
    token = uuid.uuid4()
    job = owner_session.get(Job, job_id)
    assert job is not None
    job.state = "running"
    job.attempt_count = job.max_attempts
    job.locked_by = str(token)
    job.locked_until = get_datetime_utc() - timedelta(seconds=1)
    owner_session.add(job)
    owner_session.add(
        JobAttempt(
            id=token,
            clinic_id=job.clinic_id,
            job_id=job.id,
            worker_membership_id=demo_id("membership-worker"),
            attempt_no=job.max_attempts,
        )
    )
    owner_session.commit()

    async def provider_must_not_run(*_args, **_kwargs) -> None:
        raise AssertionError("exhausted job must not re-enter provider processing")

    monkeypatch.setattr(ai_worker, "process_job", provider_must_not_run)
    assert asyncio.run(ai_worker.run_once()) == 0
    assert asyncio.run(ai_worker.run_once()) == 0

    with Session(engine) as session:
        terminal = session.get(Job, job_id)
        attempt = session.exec(
            select(JobAttempt).where(JobAttempt.job_id == job_id)
        ).one()
        assert terminal is not None
        assert terminal.state == "failed"
        assert terminal.error_code == "JOB_ATTEMPTS_EXHAUSTED"
        assert terminal.locked_by is None
        assert terminal.locked_until is None
        assert attempt.status == "failed"
        assert attempt.error_code == "WORKER_LEASE_EXPIRED"
        assert attempt.completed_at is not None
        events = session.exec(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == job_id,
                DomainEvent.event_type == "job.exhausted",
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload_json == {
            "error_code": "JOB_ATTEMPTS_EXHAUSTED",
            "attempt_count": terminal.max_attempts,
        }


def test_expired_claim_cannot_finalize_after_new_worker_reclaims(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    import app.services.ai_jobs as ai_jobs
    from app.core.config import settings

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        async def extract(
            self, payload: QualifiedRedactedText, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            del payload, context
            started.set()
            await release.wait()
            return ClinicalNoteDraft(
                summary="stale attempt output",
                facts=[],
                provider="synthetic-remote",
                model="configured-test-model",
            )

    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    monkeypatch.setattr(settings, "PRESIDIO_REQUIRED", False)
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    source = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Lease fence fixture",
            "content": "routine follow up",
        },
    ).json()
    queued = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "lease-fence"},
        json={"source_entry_version_id": source["version_id"]},
    ).json()
    job_id = uuid.UUID(queued["id"])
    monkeypatch.setattr(
        ai_jobs, "_configured_remote_provider", lambda *_: BlockingProvider()
    )

    async def scenario() -> None:
        with Session(engine) as old_worker:
            job = old_worker.get(Job, job_id)
            assert job is not None
            old_context = worker_context_for_job(old_worker, job)
            assert old_context is not None
            task = asyncio.create_task(
                ai_jobs.process_job(old_worker, old_context, job_id)
            )
            await started.wait()

            with Session(engine) as new_worker:
                current = new_worker.get(Job, job_id)
                assert current is not None
                new_context = worker_context_for_job(new_worker, current)
                assert new_context is not None
                current.locked_until = get_datetime_utc() - timedelta(seconds=1)
                new_worker.add(current)
                new_worker.commit()
                new_token = uuid.uuid4()
                reclaimed = claim_job(
                    new_worker, new_context, job_id, claim_token=new_token
                )
                assert reclaimed.locked_by == str(new_token)
                new_worker.commit()

            release.set()
            with pytest.raises(HTTPException) as lost:
                await task
            assert lost.value.detail["code"] == "JOB_CLAIM_LOST"

    asyncio.run(scenario())
    with Session(engine) as session:
        assert session.exec(select(AIRun).where(AIRun.job_id == job_id)).first() is None
        attempts = session.exec(
            select(JobAttempt).where(JobAttempt.job_id == job_id)
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "failed"
        assert attempts[0].error_code == "WORKER_LEASE_EXPIRED"


def test_revoked_worker_context_cannot_claim_new_job(
    client: TestClient, auth_headers, monkeypatch, owner_session: Session
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
            "title": "Revoked worker fixture",
            "content": "routine follow up",
        },
    ).json()
    queued = client.post(
        f"/api/v1/patients/{patient_id}/ai/ingest",
        headers=headers | {"Idempotency-Key": "revoked-worker-claim"},
        json={"source_entry_version_id": source["version_id"]},
    ).json()
    job_id = uuid.UUID(queued["id"])

    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        stale_context = worker_context_for_job(session, job)
        assert stale_context is not None
        membership = owner_session.get(ClinicMembership, demo_id("membership-worker"))
        assert membership is not None
        membership.is_active = False
        owner_session.add(membership)
        owner_session.commit()
        with pytest.raises(HTTPException) as denied:
            claim_job(session, stale_context, job_id)
        assert denied.value.status_code == 403
    from app.ai_worker import run_once

    assert asyncio.run(run_once()) == 0
