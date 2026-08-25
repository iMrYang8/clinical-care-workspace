"""Bounded PostgreSQL-backed AI job consumer.

The API only enqueues remote work. This process resolves a trusted worker
membership per clinic and lets process_job perform the SKIP LOCKED claim and
claim-token fenced finalization.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db import (
    assert_restricted_runtime_database,
    engine,
    set_rls_clinic,
)
from app.models import (
    AuditEvent,
    Clinic,
    ClinicMembership,
    DomainEvent,
    Job,
    JobAttempt,
    get_datetime_utc,
)
from app.services.ai_jobs import process_job, worker_context_for_job
from app.services.redaction import assert_presidio_runtime
from app.services.voice.worker import process_voice_job

logger = logging.getLogger(__name__)
_AI_JOB_KINDS = ("ai_ingest", "ai_reanalyze", "voice_process", "voice_reanalyze")
_SAFE_CONTROL_CODES = {
    "JOB_ATTEMPTS_EXHAUSTED",
    "JOB_CLAIM_LOST",
    "JOB_NOT_CLAIMABLE",
    "WORKER_UNAVAILABLE",
}


def _next_job(session: Session, clinic_id: uuid.UUID) -> Job | None:
    set_rls_clinic(session, clinic_id)
    now = get_datetime_utc()
    due = col(Job.next_run_at).is_(None) | (col(Job.next_run_at) <= now)
    unlocked = col(Job.locked_until).is_(None) | (col(Job.locked_until) < now)
    claimable = (col(Job.state).in_(["pending", "failed"]) & unlocked) | (
        (col(Job.state) == "running") & (col(Job.locked_until) < now)
    )
    return session.exec(
        select(Job)
        .where(
            Job.clinic_id == clinic_id,
            col(Job.kind).in_(_AI_JOB_KINDS),
            Job.attempt_count < Job.max_attempts,
            due,
            claimable,
        )
        .order_by(col(Job.created_at), col(Job.id))
        .limit(1)
    ).first()


def _finalize_exhausted_expired_leases(session: Session, clinic_id: uuid.UUID) -> int:
    """Terminalize expired final attempts without invoking a provider again."""

    set_rls_clinic(session, clinic_id)
    now = get_datetime_utc()
    jobs = session.exec(
        select(Job)
        .where(
            Job.clinic_id == clinic_id,
            col(Job.kind).in_(_AI_JOB_KINDS),
            Job.state == "running",
            col(Job.locked_until) < now,
            Job.attempt_count >= Job.max_attempts,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for job in jobs:
        attempts = session.exec(
            select(JobAttempt)
            .where(
                JobAttempt.clinic_id == clinic_id,
                JobAttempt.job_id == job.id,
                JobAttempt.status == "started",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        for attempt in attempts:
            attempt.status = "failed"
            attempt.error_code = "WORKER_LEASE_EXPIRED"
            attempt.completed_at = now
            session.add(attempt)
        latest_attempt = max(attempts, key=lambda item: item.attempt_no, default=None)
        membership = (
            session.get(ClinicMembership, latest_attempt.worker_membership_id)
            if latest_attempt is not None
            and latest_attempt.worker_membership_id is not None
            else None
        )
        actor_id = membership.user_id if membership is not None else job.created_by_id
        job.state = "failed"
        job.error_code = "JOB_ATTEMPTS_EXHAUSTED"
        job.locked_by = None
        job.locked_until = None
        job.next_run_at = None
        job.updated_at = now
        session.add(job)
        metadata: dict[str, object] = {
            "error_code": "JOB_ATTEMPTS_EXHAUSTED",
            "attempt_count": job.attempt_count,
        }
        session.add(
            AuditEvent(
                clinic_id=clinic_id,
                actor_id=actor_id,
                action="job.exhausted",
                resource_type="job",
                resource_id=job.id,
                metadata_json=metadata,
            )
        )
        session.add(
            DomainEvent(
                clinic_id=clinic_id,
                event_type="job.exhausted",
                aggregate_type="job",
                aggregate_id=job.id,
                actor_id=actor_id,
                payload_json=metadata,
            )
        )
    session.flush()
    return len(jobs)


def _safe_http_code(exc: HTTPException) -> str:
    detail = exc.detail
    candidate = detail.get("code") if isinstance(detail, dict) else None
    return (
        candidate
        if isinstance(candidate, str) and candidate in _SAFE_CONTROL_CODES
        else "JOB_PROCESSING_REJECTED"
    )


async def run_once() -> int:
    """Attempt at most one job per clinic and return the processed count."""

    if settings.FASTAPI_ENV != "development":
        assert_restricted_runtime_database()
        if settings.AI_PROVIDER == "openai" and settings.REMOTE_TEXT_EGRESS_ENABLED:
            assert_presidio_runtime(settings.PRESIDIO_NLP_MODEL)
    with Session(engine) as catalog:
        clinic_ids = catalog.exec(select(Clinic.id).order_by(col(Clinic.id))).all()

    processed = 0
    for clinic_id in clinic_ids:
        with Session(engine) as session:
            recovered = _finalize_exhausted_expired_leases(session, clinic_id)
            if recovered:
                session.commit()
                logger.info(
                    "ai_worker_recovered clinic_id=%s count=%s code=JOB_ATTEMPTS_EXHAUSTED",
                    clinic_id,
                    recovered,
                )
            job = _next_job(session, clinic_id)
            if job is None:
                continue
            context = worker_context_for_job(session, job)
            if context is None:
                logger.warning(
                    "ai_worker_skip clinic_id=%s job_id=%s code=WORKER_UNAVAILABLE",
                    clinic_id,
                    job.id,
                )
                continue
            try:
                if job.kind in {"voice_process", "voice_reanalyze"}:
                    await process_voice_job(session, context, job.id)
                else:
                    await process_job(session, context, job.id)
                processed += 1
            except HTTPException as exc:
                session.rollback()
                logger.info(
                    "ai_worker_skip clinic_id=%s job_id=%s code=%s",
                    clinic_id,
                    job.id,
                    _safe_http_code(exc),
                )
            except Exception:
                session.rollback()
                logger.error(
                    "ai_worker_error clinic_id=%s job_id=%s code=WORKER_LOOP_ERROR",
                    clinic_id,
                    job.id,
                )
    return processed


async def run_forever() -> None:
    delay = max(0.1, settings.AI_WORKER_POLL_SECONDS)
    while True:
        await run_once()
        await asyncio.sleep(delay)


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
