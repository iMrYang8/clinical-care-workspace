import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response, status

from app.api.deps import CurrentContext, SessionDep
from app.core.config import settings
from app.models import (
    AudioChunkAck,
    LiveTranscriptAvailability,
    TranscriptCorrection,
    TranscriptRevisionPublic,
    VoiceChunkStatus,
    VoiceDeviceJoin,
    VoiceDevicePublic,
    VoiceDeviceSeal,
    VoiceDeviceSealPublic,
    VoiceFinalizePublic,
    VoiceFinalizeRequest,
    VoicePublishPublic,
    VoiceReanalyzePublic,
    VoiceSessionCreate,
    VoiceSessionPublic,
)
from app.services.voice.service import (
    authorized_audio_asset,
    chunk_status,
    correct_transcript,
    create_voice_session,
    current_transcript,
    enqueue_reanalysis,
    finalize_voice_session,
    get_voice_session,
    join_voice_device,
    publish_voice_result,
    seal_voice_device,
    transcript_public,
    upload_audio_chunk,
    voice_device_public,
    voice_session_public,
)

router = APIRouter(prefix="/voice", tags=["voice"])


async def _bounded_chunk_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.VOICE_MAX_CHUNK_BYTES:
                raise HTTPException(
                    status_code=413, detail={"code": "AUDIO_CHUNK_SIZE_INVALID"}
                )
        except ValueError:
            pass
    parts: list[bytes] = []
    total = 0
    async for part in request.stream():
        total += len(part)
        if total > settings.VOICE_MAX_CHUNK_BYTES:
            raise HTTPException(
                status_code=413, detail={"code": "AUDIO_CHUNK_SIZE_INVALID"}
            )
        parts.append(part)
    return b"".join(parts)


@router.post(
    "/sessions", response_model=VoiceSessionPublic, status_code=status.HTTP_201_CREATED
)
def create_session(
    body: VoiceSessionCreate, session: SessionDep, context: CurrentContext
) -> VoiceSessionPublic:
    voice_session = create_voice_session(session, context, body)
    session.commit()
    session.refresh(voice_session)
    return voice_session_public(voice_session, patient_safe=context.role == "patient")


@router.get("/sessions/{session_id}", response_model=VoiceSessionPublic)
def session_status(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> VoiceSessionPublic:
    voice_session = get_voice_session(session, context, session_id)
    return voice_session_public(voice_session, patient_safe=context.role == "patient")


@router.post(
    "/sessions/{session_id}/devices",
    response_model=VoiceDevicePublic,
    status_code=status.HTTP_201_CREATED,
)
def join_device(
    session_id: uuid.UUID,
    body: VoiceDeviceJoin,
    session: SessionDep,
    context: CurrentContext,
) -> VoiceDevicePublic:
    voice_session = get_voice_session(session, context, session_id, lock=True)
    device = join_voice_device(session, context, voice_session, body)
    session.commit()
    session.refresh(device)
    return voice_device_public(device)


@router.put(
    "/sessions/{session_id}/devices/{device_id}/chunks/{chunk_index}",
    response_model=AudioChunkAck,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}}
                for media_type in ("audio/webm", "audio/mp4", "audio/wav")
            },
        }
    },
)
async def put_chunk(
    session_id: uuid.UUID,
    device_id: uuid.UUID,
    chunk_index: Annotated[int, Path(ge=0, le=21_600)],
    request: Request,
    session: SessionDep,
    context: CurrentContext,
    chunk_sha256: str = Header(alias="X-Chunk-SHA256", min_length=64, max_length=64),
    chunk_start_ms: int | None = Header(
        default=None, alias="X-Chunk-Start-Ms", ge=0, le=43_200_000
    ),
    chunk_end_ms: int | None = Header(
        default=None, alias="X-Chunk-End-Ms", ge=0, le=43_200_000
    ),
) -> AudioChunkAck:
    voice_session = get_voice_session(session, context, session_id, lock=True)
    payload = await _bounded_chunk_body(request)
    result = upload_audio_chunk(
        session,
        context,
        voice_session,
        device_id=device_id,
        chunk_index=chunk_index,
        payload=payload,
        declared_sha256=chunk_sha256,
        media_type=request.headers.get("content-type", ""),
        start_ms=chunk_start_ms,
        end_ms=chunk_end_ms,
    )
    session.commit()
    return result


@router.post(
    "/sessions/{session_id}/devices/{device_id}/seal",
    response_model=VoiceDeviceSealPublic,
)
def seal_device(
    session_id: uuid.UUID,
    device_id: uuid.UUID,
    body: VoiceDeviceSeal,
    session: SessionDep,
    context: CurrentContext,
) -> VoiceDeviceSealPublic:
    voice_session = get_voice_session(session, context, session_id, lock=True)
    result = seal_voice_device(
        session, context, voice_session, device_id=device_id, body=body
    )
    session.commit()
    return result


@router.get("/sessions/{session_id}/chunks/status", response_model=VoiceChunkStatus)
def get_chunk_status(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> VoiceChunkStatus:
    return chunk_status(
        session, context, get_voice_session(session, context, session_id)
    )


@router.post(
    "/sessions/{session_id}/finalize",
    response_model=VoiceFinalizePublic,
    status_code=status.HTTP_202_ACCEPTED,
)
def finalize(
    session_id: uuid.UUID,
    body: VoiceFinalizeRequest,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> VoiceFinalizePublic:
    voice_session = get_voice_session(session, context, session_id, lock=True)
    result = finalize_voice_session(
        session,
        context,
        voice_session,
        body,
        idempotency_key=idempotency_key,
    )
    session.commit()
    return result


@router.get(
    "/sessions/{session_id}/transcript", response_model=TranscriptRevisionPublic
)
def transcript(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> TranscriptRevisionPublic:
    voice_session = get_voice_session(session, context, session_id)
    return current_transcript(session, context, voice_session)


@router.post(
    "/sessions/{session_id}/transcript/correct",
    response_model=TranscriptRevisionPublic,
    status_code=status.HTTP_201_CREATED,
)
def correct(
    session_id: uuid.UUID,
    body: TranscriptCorrection,
    session: SessionDep,
    context: CurrentContext,
) -> TranscriptRevisionPublic:
    voice_session = get_voice_session(session, context, session_id, lock=True)
    revision = correct_transcript(session, context, voice_session, body)
    session.commit()
    session.refresh(voice_session)
    return transcript_public(session, voice_session, revision)


@router.post(
    "/sessions/{session_id}/reanalyze",
    response_model=VoiceReanalyzePublic,
    status_code=status.HTTP_202_ACCEPTED,
)
def reanalyze(
    session_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=200
    ),
) -> VoiceReanalyzePublic:
    voice_session = get_voice_session(session, context, session_id, lock=True)
    result = enqueue_reanalysis(
        session, context, voice_session, idempotency_key=idempotency_key
    )
    session.commit()
    return result


@router.post("/sessions/{session_id}/publish", response_model=VoicePublishPublic)
def publish(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> VoicePublishPublic:
    voice_session = get_voice_session(session, context, session_id, lock=True)
    result = publish_voice_result(session, context, voice_session)
    session.commit()
    return result


@router.get(
    "/sessions/{session_id}/audio",
    response_class=Response,
    responses={
        200: {
            "description": "Authorized normalized 16 kHz mono PCM audio",
            "content": {
                "audio/wav": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
def audio(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> Response:
    voice_session = get_voice_session(session, context, session_id)
    payload, media_type = authorized_audio_asset(session, context, voice_session)
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store", "Accept-Ranges": "none"},
    )


@router.get("/sessions/{session_id}/live", response_model=LiveTranscriptAvailability)
def live_status(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> LiveTranscriptAvailability:
    get_voice_session(session, context, session_id)
    if context.role not in {"patient", "staff", "clinician"}:
        return LiveTranscriptAvailability(
            available=False, status="unavailable", reason_code="ROLE_NOT_PERMITTED"
        )
    if not settings.LIVE_TRANSCRIPT_ENABLED:
        return LiveTranscriptAvailability(
            available=False,
            status="unavailable",
            reason_code="LIVE_TRANSCRIPT_NOT_CONFIGURED",
        )
    # No live provider/transport is shipped in this build.  The feature flag is
    # a deployment gate, not evidence that provisional captions exist.
    return LiveTranscriptAvailability(
        available=False,
        status="unavailable",
        reason_code="LIVE_TRANSCRIPT_TRANSPORT_UNAVAILABLE",
    )
