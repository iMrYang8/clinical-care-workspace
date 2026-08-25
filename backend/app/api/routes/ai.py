import uuid

from fastapi import APIRouter, Header, HTTPException

from app.api.deps import CurrentContext, SessionDep
from app.core.config import settings
from app.models import AIIngestRequest, JobPublic, get_datetime_utc
from app.services.ai_jobs import (
    create_or_replay_job,
    get_scoped_job,
    job_public,
    process_job,
    worker_context_for_job,
)
from app.services.voice.worker import process_voice_job

router = APIRouter(tags=["ai"])


def _require_ai_role(context: CurrentContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical role required")


async def _submit(
    *,
    patient_id: uuid.UUID,
    kind: str,
    body: AIIngestRequest,
    idempotency_key: str,
    session: SessionDep,
    context: CurrentContext,
) -> JobPublic:
    _require_ai_role(context)
    payload = body.model_dump(mode="json")
    job, replayed = create_or_replay_job(
        session,
        context,
        patient_id=patient_id,
        kind=kind,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    if not replayed:
        # The request path may execute the deterministic/no-egress provider for
        # the local demo. Configured remote jobs remain pending for a worker so
        # an HTTP request never waits on an external model call.
        worker_context = worker_context_for_job(session, job)
        if settings.AI_PROVIDER != "openai" and worker_context is not None:
            await process_job(session, worker_context, job.id)
        session.commit()
        session.refresh(job)
    return job_public(session, job)


@router.post("/patients/{patient_id}/ai/ingest", response_model=JobPublic)
async def ingest(
    patient_id: uuid.UUID,
    body: AIIngestRequest,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> JobPublic:
    return await _submit(
        patient_id=patient_id,
        kind="ai_ingest",
        body=body,
        idempotency_key=idempotency_key,
        session=session,
        context=context,
    )


@router.post("/patients/{patient_id}/ai/reanalyze", response_model=JobPublic)
async def reanalyze(
    patient_id: uuid.UUID,
    body: AIIngestRequest,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> JobPublic:
    return await _submit(
        patient_id=patient_id,
        kind="ai_reanalyze",
        body=body,
        idempotency_key=idempotency_key,
        session=session,
        context=context,
    )


@router.get("/jobs/{job_id}", response_model=JobPublic)
def get_job(
    job_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> JobPublic:
    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Role cannot inspect jobs")
    return job_public(session, get_scoped_job(session, context, job_id))


@router.post("/jobs/{job_id}/retry", response_model=JobPublic)
async def retry_job(
    job_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> JobPublic:
    _require_ai_role(context)
    # Serialize retry against worker claim/recovery and refresh any identity-map
    # copy before rechecking state and the attempt budget.
    job = get_scoped_job(session, context, job_id, lock=True)
    if job.state != "failed":
        raise HTTPException(
            status_code=409,
            detail={"code": "JOB_NOT_RETRYABLE", "state": job.state},
        )
    if job.attempt_count >= job.max_attempts:
        raise HTTPException(status_code=409, detail={"code": "JOB_ATTEMPTS_EXHAUSTED"})
    worker_context = worker_context_for_job(session, job)
    if worker_context is None:
        raise HTTPException(status_code=503, detail={"code": "WORKER_UNAVAILABLE"})
    if settings.AI_PROVIDER == "openai":
        job.state = "pending"
        job.error_code = None
        job.updated_at = get_datetime_utc()
        session.add(job)
    else:
        if job.kind in {"voice_process", "voice_reanalyze"}:
            await process_voice_job(session, worker_context, job.id)
        else:
            await process_job(session, worker_context, job.id)
    session.commit()
    session.refresh(job)
    return job_public(session, job)
