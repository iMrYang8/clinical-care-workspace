"""Persist and evaluate the live AI-worker deployment capability."""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from app.core.config import settings
from app.models import WorkerHeartbeat, get_datetime_utc

AI_WORKER_KIND = "ai-worker"
AI_WORKER_VERSION = "nightingale-ai-worker-v1"


def record_ai_worker_heartbeat(session: Session) -> WorkerHeartbeat:
    """Upsert the process heartbeat and exact worker contract version."""

    row = session.exec(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_kind == AI_WORKER_KIND)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        row = WorkerHeartbeat(
            worker_kind=AI_WORKER_KIND,
            worker_version=AI_WORKER_VERSION,
            source_commit=settings.NIGHTINGALE_SOURCE_COMMIT,
        )
    else:
        row.worker_version = AI_WORKER_VERSION
        row.source_commit = settings.NIGHTINGALE_SOURCE_COMMIT
        row.updated_at = get_datetime_utc()
    session.add(row)
    session.flush()
    return row


def ensure_development_worker_heartbeat(session: Session) -> None:
    """Create one explicit persisted fixture heartbeat for local acceptance tests."""

    if settings.FASTAPI_ENV != "development" or not settings.ENABLE_DEMO_AUTH:
        return
    existing = session.exec(
        select(WorkerHeartbeat).where(WorkerHeartbeat.worker_kind == AI_WORKER_KIND)
    ).first()
    if existing is None:
        record_ai_worker_heartbeat(session)


def ai_worker_capability(session: Session) -> tuple[bool, str | None]:
    """Require deployment enablement, exact version, and a fresh heartbeat."""

    if not settings.AI_WORKER_ENABLED or settings.AI_WORKER_POLL_SECONDS <= 0:
        return False, "worker_capability_disabled"
    ensure_development_worker_heartbeat(session)
    heartbeat = session.exec(
        select(WorkerHeartbeat).where(WorkerHeartbeat.worker_kind == AI_WORKER_KIND)
    ).first()
    if heartbeat is None:
        return False, "worker_heartbeat_missing"
    if heartbeat.worker_version != AI_WORKER_VERSION:
        return False, "worker_version_mismatch"
    cutoff = get_datetime_utc() - timedelta(
        seconds=settings.AI_WORKER_HEARTBEAT_MAX_AGE_SECONDS
    )
    if heartbeat.updated_at < cutoff:
        return False, "worker_heartbeat_stale"
    return True, None
