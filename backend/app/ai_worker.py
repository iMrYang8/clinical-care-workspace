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
from app.core.db import engine, set_rls_clinic
from app.models import Clinic, Job, get_datetime_utc
from app.services.ai_jobs import process_job, worker_context_for_job

logger = logging.getLogger(__name__)
_AI_JOB_KINDS = ("ai_ingest", "ai_reanalyze")
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

    with Session(engine) as catalog:
        clinic_ids = catalog.exec(select(Clinic.id).order_by(col(Clinic.id))).all()

    processed = 0
    for clinic_id in clinic_ids:
        with Session(engine) as session:
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
