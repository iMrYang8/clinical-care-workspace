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
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db import (
    assert_restricted_runtime_database,
    engine,
    set_rls_actor,
    set_rls_clinic,
)
from app.models import (
    AuditEvent,
    Clinic,
    ClinicMembership,
    ClinicOperationalSetting,
    DomainEvent,
    Job,
    JobAttempt,
    User,
    VoiceSession,
    get_datetime_utc,
)
from app.services.ai_jobs import process_job, worker_context_for_job
from app.services.messaging import (
    dispatch_due_notifications,
    recover_stale_notifications,
)
from app.services.redaction import assert_presidio_runtime
from app.services.voice.worker import process_voice_job
from app.services.worker_heartbeat import record_ai_worker_heartbeat

logger = logging.getLogger(__name__)
_AI_JOB_KINDS = (
    "ai_ingest",
    "ai_reanalyze",
    "ai_recovery",
    "voice_process",
    "voice_reanalyze",
)
_SAFE_CONTROL_CODES = {
    "JOB_ATTEMPTS_EXHAUSTED",
    "JOB_CLAIM_LOST",
    "JOB_NOT_CLAIMABLE",
    "WORKER_UNAVAILABLE",
}
_VOICE_PROCESSING_STATES = {
    "finalizing",
    "assembling",
    "preprocessing",
    "transcribing",
    "redacting",
    "extracting",
}
_VOICE_EXHAUSTED_CODE = "VOICE_WORKER_ATTEMPTS_EXHAUSTED"


def _bind_worker_context(session: Session, clinic_id: uuid.UUID) -> bool:
    """Bind one live service identity before any tenant-table read.

    PostgreSQL uses a narrow SECURITY DEFINER lookup that returns identifiers
    only after validating the active worker user and membership.  SQLite is
    retained solely for deterministic unit tests, where row-level security is
    not available.
    """

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
        membership, user = sqlite_row
        del membership
        user_id = user.id
    set_rls_clinic(session, clinic_id)
    set_rls_actor(session, user_id, role="worker")
    operational = session.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == clinic_id
        )
    ).first()
    return operational is None or operational.worker_enabled


def _next_job(session: Session, clinic_id: uuid.UUID) -> Job | None:
    set_rls_clinic(session, clinic_id)
    now = get_datetime_utc()
    unlocked = col(Job.locked_until).is_(None) | (col(Job.locked_until) < now)
    # Pending rows represent an initial submission or explicit manual retry.
    # Failed rows are automatic work only when the failure path persisted a
    # due retry time (for example the bounded provider retry ladder). Keeping
    # this selector identical to ``claim_job`` prevents permanent failures
    # with next_run_at=NULL from spinning in the worker loop.
    claimable = (
        ((col(Job.state) == "pending") & unlocked)
        | (
            (col(Job.state) == "failed")
            & unlocked
            & col(Job.next_run_at).is_not(None)
            & (col(Job.next_run_at) <= now)
        )
        | ((col(Job.state) == "running") & (col(Job.locked_until) < now))
    )
    return session.exec(
        select(Job)
        .where(
            Job.clinic_id == clinic_id,
            col(Job.kind).in_(_AI_JOB_KINDS),
            Job.attempt_count < Job.max_attempts,
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
        if job.kind in {"voice_process", "voice_reanalyze"}:
            voice_session = session.exec(
                select(VoiceSession)
                .where(
                    VoiceSession.clinic_id == clinic_id,
                    VoiceSession.processing_job_id == job.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if (
                voice_session is not None
                and voice_session.state in _VOICE_PROCESSING_STATES
            ):
                voice_session.state = "needs_review"
                voice_session.error_code = _VOICE_EXHAUSTED_CODE
                voice_session.warning_codes_json = sorted(
                    {*voice_session.warning_codes_json, _VOICE_EXHAUSTED_CODE}
                )
                voice_session.updated_at = now
                session.add(voice_session)
                metadata["voice_session_id"] = str(voice_session.id)
                metadata["voice_error_code"] = _VOICE_EXHAUSTED_CODE
        session.add(
            AuditEvent(
                clinic_id=clinic_id,
                actor_id=actor_id,
                action="job.exhausted",
                resource_type="job",
                resource_id=job.id,
                reason_code="job_attempts_exhausted",
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
        record_ai_worker_heartbeat(catalog)
        clinic_ids = catalog.exec(select(Clinic.id).order_by(col(Clinic.id))).all()
        catalog.commit()

    processed = 0
    for clinic_id in clinic_ids:
        with Session(engine) as session:
            if not _bind_worker_context(session, clinic_id):
                logger.warning("ai_worker_skip code=WORKER_UNAVAILABLE")
                continue
            recovered = _finalize_exhausted_expired_leases(session, clinic_id)
            if recovered:
                session.commit()
                logger.info(
                    "ai_worker_recovered count=%s code=JOB_ATTEMPTS_EXHAUSTED",
                    recovered,
                )
                if not _bind_worker_context(session, clinic_id):
                    continue
            stale_deliveries = recover_stale_notifications(
                session, clinic_id=clinic_id, limit=100
            )
            if stale_deliveries:
                session.commit()
                logger.info(
                    "notification_recovered count=%s code=CALLBACK_SILENT",
                    stale_deliveries,
                )
                if not _bind_worker_context(session, clinic_id):
                    continue
            dispatched = dispatch_due_notifications(
                session, clinic_id=clinic_id, limit=25
            )
            if dispatched:
                session.commit()
                logger.info("notification_dispatch count=%s", dispatched)
                if not _bind_worker_context(session, clinic_id):
                    continue
            job = _next_job(session, clinic_id)
            if job is None:
                continue
            context = worker_context_for_job(session, job)
            if context is None:
                logger.warning("ai_worker_skip code=WORKER_UNAVAILABLE")
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
                    "ai_worker_skip code=%s",
                    _safe_http_code(exc),
                )
            except Exception:
                session.rollback()
                logger.error("ai_worker_error code=WORKER_LOOP_ERROR")
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
