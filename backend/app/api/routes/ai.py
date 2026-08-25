import uuid

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import select

from app.api.deps import CurrentContext, SessionDep
from app.core.config import settings
from app.models import AIIngestRequest, Job, JobPublic, VoiceSession, get_datetime_utc
from app.services.ai_jobs import (
    create_or_replay_job,
    get_scoped_job,
    job_public,
    process_job,
    worker_context_for_job,
)

router = APIRouter(tags=["ai"])


def _require_ai_role(context: CurrentContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical role required")


def _lock_retryable_voice_session(
    session: SessionDep,
    context: CurrentContext,
    job: Job,
    *,
    provider_pending_retry: bool,
) -> VoiceSession:
    voice_session = session.exec(
        select(VoiceSession)
        .where(
            VoiceSession.clinic_id == context.clinic_id,
            VoiceSession.processing_job_id == job.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if voice_session is None:
        raise HTTPException(
            status_code=409, detail={"code": "VOICE_JOB_SESSION_NOT_ACTIVE"}
        )
    if (
        voice_session.state == "published"
        or voice_session.published_entry_id is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_ALREADY_PUBLISHED"})
    processing_states = {
        "finalizing",
        "assembling",
        "preprocessing",
        "transcribing",
        "redacting",
        "extracting",
    }
    if provider_pending_retry:
        if voice_session.state != "needs_review":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VOICE_RETRY_STATE_CONFLICT",
                    "state": voice_session.state,
                },
            )
        if (
            voice_session.current_transcript_revision_id is not None
            or "TRANSCRIPT_PENDING" not in voice_session.warning_codes_json
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "VOICE_TRANSCRIPT_ALREADY_AVAILABLE"},
            )
    elif voice_session.state not in processing_states | {"needs_review"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_RETRY_STATE_CONFLICT",
                "state": voice_session.state,
            },
        )
    if job.kind == "voice_reanalyze" and voice_session.state == "needs_review":
        # Legacy/previously failed reanalysis rows may have exposed the old
        # non-stale revision while the durable job was queued again. Move the
        # session back behind the publication/correction CAS barrier in the
        # same transaction that makes the job pending.
        voice_session.state = "extracting"
        voice_session.updated_at = get_datetime_utc()
        session.add(voice_session)
    return voice_session


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
    voice_review_retry = job.state == "needs_review" and job.kind == "voice_process"
    if job.state != "failed" and not voice_review_retry:
        raise HTTPException(
            status_code=409,
            detail={"code": "JOB_NOT_RETRYABLE", "state": job.state},
        )
    if job.attempt_count >= job.max_attempts:
        raise HTTPException(status_code=409, detail={"code": "JOB_ATTEMPTS_EXHAUSTED"})
    if job.kind in {"voice_process", "voice_reanalyze"}:
        _lock_retryable_voice_session(
            session,
            context,
            job,
            provider_pending_retry=voice_review_retry,
        )
    worker_context = worker_context_for_job(session, job)
    if worker_context is None:
        raise HTTPException(status_code=503, detail={"code": "WORKER_UNAVAILABLE"})
    if voice_review_retry:
        # ``claim_job`` deliberately excludes terminal review rows from the
        # background poller. Only this explicit clinical action makes it
        # claimable again after an ASR provider is configured.
        job.state = "failed"
        job.updated_at = get_datetime_utc()
        session.add(job)
        session.flush()
    if job.kind in {"voice_process", "voice_reanalyze"}:
        # Voice work always crosses the durable worker boundary.  Text-provider
        # selection must never decide whether FFmpeg/ASR runs in an API request.
        job.state = "pending"
        job.error_code = None
        job.updated_at = get_datetime_utc()
        session.add(job)
    elif settings.AI_PROVIDER == "openai":
        job.state = "pending"
        job.error_code = None
        job.updated_at = get_datetime_utc()
        session.add(job)
    else:
        await process_job(session, worker_context, job.id)
    session.commit()
    session.refresh(job)
    return job_public(session, job)
