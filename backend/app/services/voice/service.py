from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    AudioAsset,
    AudioChunk,
    AudioChunkAck,
    ClinicalFact,
    ClinicalFactPublic,
    Entry,
    EntryRelation,
    EntryVersion,
    ProvenancePointer,
    TranscriptCorrection,
    TranscriptRevision,
    TranscriptRevisionPublic,
    TranscriptSegment,
    TranscriptSegmentPublic,
    VoiceChunkStatus,
    VoiceDevice,
    VoiceDeviceAbandonPublic,
    VoiceDeviceChunkStatus,
    VoiceDeviceJoin,
    VoiceDevicePublic,
    VoiceDeviceSeal,
    VoiceDeviceSealPublic,
    VoiceFinalizePublic,
    VoiceFinalizeRequest,
    VoicePublishPublic,
    VoiceReanalyzePublic,
    VoiceSession,
    VoiceSessionCreate,
    VoiceSessionPublic,
    get_datetime_utc,
)
from app.services.ai_jobs import create_or_replay_job
from app.services.nightingale import emit_change, get_patient
from app.services.voice.provenance import validate_fact_evidence

_CAPTURE_STATES = {"created", "recording"}
_CLINICAL_ROLES = {"staff", "clinician"}


def _chunk_replay_matches(
    existing: AudioChunk,
    *,
    digest: str,
    byte_length: int,
    media_type: str,
    start_ms: int | None,
    end_ms: int | None,
) -> bool:
    return (
        existing.plaintext_sha256 == digest
        and existing.byte_length == byte_length
        and existing.media_type == media_type
        and existing.start_ms == start_ms
        and existing.end_ms == end_ms
    )


def _patient_summary(session_row: VoiceSession) -> str | None:
    if session_row.patient_summary_ciphertext is None:
        return None
    return field_codec.decrypt_text(
        session_row.clinic_id,
        "voice_session.patient_summary",
        session_row.id,
        session_row.patient_summary_ciphertext,
    )


def voice_session_public(
    session_row: VoiceSession, *, patient_safe: bool
) -> VoiceSessionPublic:
    return VoiceSessionPublic(
        id=session_row.id,
        patient_id=session_row.patient_id,
        capture_kind=session_row.capture_kind,
        state=session_row.state,
        patient_summary=_patient_summary(session_row),
        warning_codes=[] if patient_safe else session_row.warning_codes_json,
        error_code=None if patient_safe else session_row.error_code,
        current_transcript_revision_id=(
            None if patient_safe else session_row.current_transcript_revision_id
        ),
        published_entry_id=session_row.published_entry_id,
        created_at=session_row.created_at,
        updated_at=session_row.updated_at,
    )


def get_voice_session(
    db: Session,
    context: RequestContext,
    session_id: uuid.UUID,
    *,
    lock: bool = False,
) -> VoiceSession:
    statement = select(VoiceSession).where(
        VoiceSession.clinic_id == context.clinic_id, VoiceSession.id == session_id
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    voice_session = db.exec(statement).first()
    if voice_session is None:
        raise HTTPException(status_code=404, detail="Voice session not found")
    get_patient(db, context, voice_session.patient_id)
    return voice_session


def _authorize_capture(context: RequestContext, capture_kind: str) -> None:
    if capture_kind == "patient" and context.role == "patient":
        return
    if capture_kind == "clinical" and context.role in _CLINICAL_ROLES:
        return
    raise HTTPException(status_code=403, detail="Role cannot create this capture kind")


def _authorize_session_write(
    context: RequestContext, voice_session: VoiceSession
) -> None:
    _authorize_capture(context, voice_session.capture_kind)
    if context.role == "patient" and voice_session.created_by_id != context.user_id:
        raise HTTPException(
            status_code=403, detail="Patient capture belongs to another user"
        )


def _require_current_revision(
    voice_session: VoiceSession, expected_revision_id: uuid.UUID
) -> uuid.UUID:
    current_revision_id = voice_session.current_transcript_revision_id
    if current_revision_id is None:
        raise HTTPException(status_code=409, detail={"code": "TRANSCRIPT_NOT_READY"})
    if current_revision_id != expected_revision_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TRANSCRIPT_REVISION_CONFLICT",
                "expected_revision_id": str(expected_revision_id),
                "current_revision_id": str(current_revision_id),
            },
        )
    return current_revision_id


def create_voice_session(
    db: Session, context: RequestContext, body: VoiceSessionCreate
) -> VoiceSession:
    get_patient(db, context, body.patient_id)
    _authorize_capture(context, body.capture_kind)
    if body.synthetic_fixture:
        if (
            settings.FASTAPI_ENV != "development"
            or not settings.ENABLE_DEMO_AUTH
            or body.fixture_id != "code-switch-overlap-v1"
        ):
            raise HTTPException(
                status_code=403, detail={"code": "SYNTHETIC_FIXTURE_DISABLED"}
            )
    elif body.fixture_id is not None:
        raise HTTPException(
            status_code=422, detail={"code": "FIXTURE_ID_REQUIRES_SYNTHETIC_MODE"}
        )
    voice_session = VoiceSession(
        clinic_id=context.clinic_id,
        patient_id=body.patient_id,
        capture_kind=body.capture_kind,
        synthetic_fixture=body.synthetic_fixture,
        fixture_id=body.fixture_id,
        created_by_id=context.user_id,
    )
    db.add(voice_session)
    db.flush()
    emit_change(
        db,
        context,
        action="voice.session_created",
        resource_type="voice_session",
        resource_id=voice_session.id,
        metadata={"capture_kind": body.capture_kind, "state": "created"},
    )
    return voice_session


def join_voice_device(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    body: VoiceDeviceJoin,
) -> VoiceDevice:
    if (
        voice_session.patient_id != body.expected_patient_id
        or voice_session.capture_kind != body.expected_capture_kind
    ):
        raise HTTPException(
            status_code=409, detail={"code": "VOICE_SESSION_CONTEXT_MISMATCH"}
        )
    _authorize_session_write(context, voice_session)
    if voice_session.state not in _CAPTURE_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_SESSION_NOT_RECORDING",
                "state": voice_session.state,
            },
        )
    trusted_capture_role = context.role
    if trusted_capture_role not in {"patient", "staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Role cannot join a capture")
    existing = db.exec(
        select(VoiceDevice).where(
            VoiceDevice.clinic_id == context.clinic_id,
            VoiceDevice.session_id == voice_session.id,
            VoiceDevice.client_device_id == body.client_device_id,
        )
    ).first()
    if existing is not None:
        if existing.joined_by_id != context.user_id:
            raise HTTPException(status_code=409, detail={"code": "DEVICE_ID_IN_USE"})
        return existing
    device_count = len(
        db.exec(
            select(VoiceDevice.id).where(
                VoiceDevice.clinic_id == context.clinic_id,
                VoiceDevice.session_id == voice_session.id,
            )
        ).all()
    )
    if device_count >= 8:
        raise HTTPException(
            status_code=409, detail={"code": "VOICE_DEVICE_LIMIT_REACHED"}
        )
    device = VoiceDevice(
        clinic_id=context.clinic_id,
        session_id=voice_session.id,
        client_device_id=body.client_device_id,
        capture_role=trusted_capture_role,
        joined_by_id=context.user_id,
    )
    db.add(device)
    voice_session.state = "recording"
    voice_session.updated_at = get_datetime_utc()
    db.add(voice_session)
    db.flush()
    emit_change(
        db,
        context,
        action="voice.device_joined",
        resource_type="voice_device",
        resource_id=device.id,
        metadata={"session_id": str(voice_session.id)},
    )
    return device


def voice_device_public(device: VoiceDevice) -> VoiceDevicePublic:
    return VoiceDevicePublic(
        id=device.id,
        session_id=device.session_id,
        client_device_id=device.client_device_id,
        capture_role=device.capture_role,
        created_at=device.created_at,
    )


def abandon_empty_voice_device(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    *,
    device_id: uuid.UUID,
) -> VoiceDeviceAbandonPublic:
    _authorize_session_write(context, voice_session)
    if voice_session.state not in _CAPTURE_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_SESSION_NOT_RECORDING",
                "state": voice_session.state,
            },
        )
    device = _get_device(db, context, voice_session.id, device_id)
    chunk_count = db.exec(
        select(func.count(col(AudioChunk.id))).where(
            AudioChunk.clinic_id == context.clinic_id,
            AudioChunk.session_id == voice_session.id,
            AudioChunk.device_id == device.id,
        )
    ).one()
    if int(chunk_count) != 0 or device.last_declared_chunk_index is not None:
        raise HTTPException(status_code=409, detail={"code": "VOICE_DEVICE_HAS_AUDIO"})
    db.delete(device)
    emit_change(
        db,
        context,
        action="voice.device_abandoned",
        resource_type="voice_device",
        resource_id=device.id,
        metadata={"session_id": str(voice_session.id)},
    )
    return VoiceDeviceAbandonPublic(device_id=device.id)


def _get_device(
    db: Session,
    context: RequestContext,
    session_id: uuid.UUID,
    device_id: uuid.UUID,
) -> VoiceDevice:
    device = db.exec(
        select(VoiceDevice).where(
            VoiceDevice.clinic_id == context.clinic_id,
            VoiceDevice.session_id == session_id,
            VoiceDevice.id == device_id,
        )
    ).first()
    if device is None:
        raise HTTPException(status_code=404, detail="Voice device not found")
    if device.joined_by_id != context.user_id:
        raise HTTPException(status_code=403, detail="Device belongs to another member")
    return device


def upload_audio_chunk(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    *,
    device_id: uuid.UUID,
    chunk_index: int,
    payload: bytes,
    declared_sha256: str,
    media_type: str,
    start_ms: int | None,
    end_ms: int | None,
) -> AudioChunkAck:
    _authorize_session_write(context, voice_session)
    device = _get_device(db, context, voice_session.id, device_id)
    if not payload or len(payload) > settings.VOICE_MAX_CHUNK_BYTES:
        raise HTTPException(
            status_code=413, detail={"code": "AUDIO_CHUNK_SIZE_INVALID"}
        )
    digest = hashlib.sha256(payload).hexdigest()
    if digest != declared_sha256.lower():
        raise HTTPException(
            status_code=422, detail={"code": "AUDIO_CHUNK_HASH_INVALID"}
        )
    normalized_type = media_type.split(";", 1)[0].strip().lower()
    if normalized_type not in {"audio/webm", "audio/mp4", "audio/wav", "audio/x-wav"}:
        raise HTTPException(
            status_code=415, detail={"code": "AUDIO_MEDIA_TYPE_INVALID"}
        )
    if (start_ms is None) != (end_ms is None) or (
        start_ms is not None and end_ms is not None and end_ms <= start_ms
    ):
        raise HTTPException(
            status_code=422, detail={"code": "AUDIO_CHUNK_TIME_INVALID"}
        )
    existing = db.exec(
        select(AudioChunk).where(
            AudioChunk.clinic_id == context.clinic_id,
            AudioChunk.device_id == device_id,
            AudioChunk.chunk_index == chunk_index,
        )
    ).first()
    if existing is not None:
        if existing.plaintext_sha256 != digest:
            raise HTTPException(
                status_code=409, detail={"code": "AUDIO_CHUNK_HASH_CONFLICT"}
            )
        if not _chunk_replay_matches(
            existing,
            digest=digest,
            byte_length=len(payload),
            media_type=normalized_type,
            start_ms=start_ms,
            end_ms=end_ms,
        ):
            raise HTTPException(
                status_code=409, detail={"code": "AUDIO_CHUNK_METADATA_CONFLICT"}
            )
        return AudioChunkAck(chunk_index=chunk_index, duplicate=True)

    # A successfully acknowledged PUT remains idempotent even after the
    # device seals or the session advances. Only a new index is constrained by
    # capture state; otherwise a lost response retried after finalize would be
    # reported as a false failure.
    if voice_session.state not in _CAPTURE_STATES:
        raise HTTPException(
            status_code=409, detail={"code": "VOICE_SESSION_NOT_RECORDING"}
        )
    if device.last_declared_chunk_index is not None:
        raise HTTPException(status_code=409, detail={"code": "VOICE_DEVICE_SEALED"})

    session_bytes = db.exec(
        select(func.coalesce(func.sum(AudioChunk.byte_length), 0)).where(
            AudioChunk.clinic_id == context.clinic_id,
            AudioChunk.session_id == voice_session.id,
        )
    ).one()
    if int(session_bytes) + len(payload) > settings.VOICE_MAX_SESSION_BYTES:
        raise HTTPException(
            status_code=413, detail={"code": "VOICE_SESSION_SIZE_LIMIT_REACHED"}
        )

    chunk_id = uuid.uuid4()
    chunk = AudioChunk(
        id=chunk_id,
        clinic_id=context.clinic_id,
        session_id=voice_session.id,
        device_id=device_id,
        chunk_index=chunk_index,
        payload_ciphertext=field_codec.encrypt(
            context.clinic_id, "audio_chunk.payload", chunk_id, payload
        ),
        plaintext_sha256=digest,
        byte_length=len(payload),
        media_type=normalized_type,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    try:
        with db.begin_nested():
            db.add(chunk)
            db.flush()
    except IntegrityError:
        existing = db.exec(
            select(AudioChunk).where(
                AudioChunk.clinic_id == context.clinic_id,
                AudioChunk.device_id == device_id,
                AudioChunk.chunk_index == chunk_index,
            )
        ).first()
        if existing is None or existing.plaintext_sha256 != digest:
            raise HTTPException(
                status_code=409, detail={"code": "AUDIO_CHUNK_HASH_CONFLICT"}
            )
        if not _chunk_replay_matches(
            existing,
            digest=digest,
            byte_length=len(payload),
            media_type=normalized_type,
            start_ms=start_ms,
            end_ms=end_ms,
        ):
            raise HTTPException(
                status_code=409, detail={"code": "AUDIO_CHUNK_METADATA_CONFLICT"}
            )
        return AudioChunkAck(chunk_index=chunk_index, duplicate=True)
    return AudioChunkAck(chunk_index=chunk_index)


def seal_voice_device(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    device_id: uuid.UUID,
    body: VoiceDeviceSeal,
) -> VoiceDeviceSealPublic:
    _authorize_session_write(context, voice_session)
    device = _get_device(db, context, voice_session.id, device_id)
    if device.last_declared_chunk_index is not None:
        if device.last_declared_chunk_index != body.last_chunk_index:
            raise HTTPException(
                status_code=409, detail={"code": "VOICE_DEVICE_SEAL_CONFLICT"}
            )
        return VoiceDeviceSealPublic(
            device_id=device.id,
            last_chunk_index=device.last_declared_chunk_index,
        )
    if voice_session.state not in _CAPTURE_STATES:
        raise HTTPException(
            status_code=409, detail={"code": "VOICE_SESSION_NOT_RECORDING"}
        )
    received = set(
        db.exec(
            select(AudioChunk.chunk_index).where(
                AudioChunk.clinic_id == context.clinic_id,
                AudioChunk.device_id == device.id,
            )
        ).all()
    )
    expected = set(range(body.last_chunk_index + 1))
    missing = sorted(expected - received)
    if missing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MISSING_AUDIO_CHUNKS",
                "missing": {str(device.id): missing},
            },
        )
    if received - expected:
        raise HTTPException(
            status_code=409, detail={"code": "AUDIO_CHUNKS_AFTER_DECLARED_END"}
        )
    device.last_declared_chunk_index = body.last_chunk_index
    db.add(device)
    emit_change(
        db,
        context,
        action="voice.device_sealed",
        resource_type="voice_device",
        resource_id=device.id,
        metadata={
            "session_id": str(voice_session.id),
            "last_chunk_index": body.last_chunk_index,
        },
    )
    return VoiceDeviceSealPublic(
        device_id=device.id, last_chunk_index=body.last_chunk_index
    )


def chunk_status(
    db: Session, context: RequestContext, voice_session: VoiceSession
) -> VoiceChunkStatus:
    _authorize_session_write(context, voice_session)
    devices = db.exec(
        select(VoiceDevice)
        .where(
            VoiceDevice.clinic_id == context.clinic_id,
            VoiceDevice.session_id == voice_session.id,
        )
        .order_by(col(VoiceDevice.created_at), col(VoiceDevice.id))
    ).all()
    rows: list[VoiceDeviceChunkStatus] = []
    total = 0
    for device in devices:
        indices = db.exec(
            select(AudioChunk.chunk_index)
            .where(
                AudioChunk.clinic_id == context.clinic_id,
                AudioChunk.device_id == device.id,
            )
            .order_by(col(AudioChunk.chunk_index))
        ).all()
        total += len(indices)
        rows.append(
            VoiceDeviceChunkStatus(
                device_id=device.id,
                client_device_id=device.client_device_id,
                received_indices=list(indices),
                last_declared_chunk_index=device.last_declared_chunk_index,
            )
        )
    return VoiceChunkStatus(uploaded_chunks=total, devices=rows)


def finalize_voice_session(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    body: VoiceFinalizeRequest,
    *,
    idempotency_key: str,
) -> VoiceFinalizePublic:
    _authorize_session_write(context, voice_session)
    if voice_session.state not in {"recording", "finalizing"}:
        if voice_session.processing_job_id is not None:
            return VoiceFinalizePublic(
                session_id=voice_session.id,
                state=voice_session.state,
                job_id=voice_session.processing_job_id,
            )
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_FINALIZABLE"})
    if len({item.device_id for item in body.devices}) != len(body.devices):
        raise HTTPException(status_code=422, detail={"code": "DUPLICATE_DEVICE"})
    declared_ids = {item.device_id for item in body.devices}
    session_devices = db.exec(
        select(VoiceDevice).where(
            VoiceDevice.clinic_id == context.clinic_id,
            VoiceDevice.session_id == voice_session.id,
        )
    ).all()
    session_device_ids = {item.id for item in session_devices}
    undeclared = sorted(str(item) for item in session_device_ids - declared_ids)
    if undeclared:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_DEVICE_DECLARATIONS_INCOMPLETE",
                "missing_device_ids": undeclared,
            },
        )
    unknown = sorted(str(item) for item in declared_ids - session_device_ids)
    if unknown:
        raise HTTPException(status_code=404, detail="Voice device not found")
    missing: dict[str, list[int]] = {}
    trusted_devices: list[dict[str, object]] = []
    for declaration in body.devices:
        device = db.exec(
            select(VoiceDevice).where(
                VoiceDevice.clinic_id == context.clinic_id,
                VoiceDevice.session_id == voice_session.id,
                VoiceDevice.id == declaration.device_id,
            )
        ).first()
        if device is None:
            raise HTTPException(status_code=404, detail="Voice device not found")
        received = set(
            db.exec(
                select(AudioChunk.chunk_index).where(
                    AudioChunk.clinic_id == context.clinic_id,
                    AudioChunk.device_id == device.id,
                )
            ).all()
        )
        absent = [
            index
            for index in range(declaration.last_chunk_index + 1)
            if index not in received
        ]
        if absent:
            missing[str(device.id)] = absent
        trusted_devices.append(
            {
                "device_id": str(device.id),
                "last_chunk_index": declaration.last_chunk_index,
            }
        )
    if missing:
        raise HTTPException(
            status_code=409, detail={"code": "MISSING_AUDIO_CHUNKS", "missing": missing}
        )
    unsealed = sorted(
        str(item.id)
        for item in session_devices
        if item.last_declared_chunk_index is None
    )
    if unsealed:
        raise HTTPException(
            status_code=409,
            detail={"code": "VOICE_DEVICES_NOT_SEALED", "device_ids": unsealed},
        )
    declarations_by_id = {item.device_id: item for item in body.devices}
    if any(
        item.last_declared_chunk_index != declarations_by_id[item.id].last_chunk_index
        for item in session_devices
    ):
        raise HTTPException(
            status_code=409, detail={"code": "VOICE_DEVICE_SEAL_CONFLICT"}
        )
    job, _replayed = create_or_replay_job(
        db,
        context,
        patient_id=voice_session.patient_id,
        kind="voice_process",
        idempotency_key=idempotency_key,
        payload={"session_id": str(voice_session.id), "devices": trusted_devices},
    )
    if voice_session.processing_job_id not in {None, job.id}:
        raise HTTPException(status_code=409, detail={"code": "VOICE_JOB_ALREADY_BOUND"})
    voice_session.processing_job_id = job.id
    voice_session.state = "finalizing"
    voice_session.updated_at = get_datetime_utc()
    db.add(voice_session)
    emit_change(
        db,
        context,
        action="voice.finalized",
        resource_type="voice_session",
        resource_id=voice_session.id,
        metadata={"job_id": str(job.id), "state": "finalizing"},
    )
    return VoiceFinalizePublic(
        session_id=voice_session.id, state=voice_session.state, job_id=job.id
    )


def _clinical_fact_public(fact: ClinicalFact, *, stale: bool) -> ClinicalFactPublic:
    return ClinicalFactPublic(
        id=fact.id,
        ordinal=fact.ordinal,
        fact_type=fact.fact_type,
        value=field_codec.decrypt_text(
            fact.clinic_id, "clinical_fact.value", fact.id, fact.value_ciphertext
        ),
        exact_quote=field_codec.decrypt_text(
            fact.clinic_id,
            "clinical_fact.exact_quote",
            fact.id,
            fact.exact_quote_ciphertext,
        ),
        transcript_start=fact.transcript_start,
        transcript_end=fact.transcript_end,
        audio_asset_id=fact.audio_asset_id,
        audio_start_ms=fact.audio_start_ms,
        audio_end_ms=fact.audio_end_ms,
        status=fact.status,
        stale=stale or fact.stale,
    )


def transcript_public(
    db: Session, voice_session: VoiceSession, revision: TranscriptRevision
) -> TranscriptRevisionPublic:
    text = field_codec.decrypt_text(
        revision.clinic_id,
        "transcript_revision.text",
        revision.id,
        revision.text_ciphertext,
    )
    summary = (
        field_codec.decrypt_text(
            revision.clinic_id,
            "transcript_revision.summary",
            revision.id,
            revision.summary_ciphertext,
        )
        if revision.summary_ciphertext is not None
        else None
    )
    segments = db.exec(
        select(TranscriptSegment)
        .where(
            TranscriptSegment.clinic_id == revision.clinic_id,
            TranscriptSegment.revision_id == revision.id,
        )
        .order_by(col(TranscriptSegment.ordinal))
    ).all()
    facts = db.exec(
        select(ClinicalFact)
        .where(
            ClinicalFact.clinic_id == revision.clinic_id,
            ClinicalFact.revision_id == revision.id,
        )
        .order_by(col(ClinicalFact.ordinal))
    ).all()
    stale = (
        revision.id != voice_session.current_transcript_revision_id or revision.stale
    )
    return TranscriptRevisionPublic(
        id=revision.id,
        session_id=revision.session_id,
        revision_no=revision.revision_no,
        previous_revision_id=revision.previous_revision_id,
        text=text,
        text_sha256=revision.text_sha256,
        summary=summary,
        provider=revision.provider,
        model=revision.model,
        detected_language=revision.detected_language,
        status=revision.status,
        needs_review=revision.needs_review,
        stale=stale,
        fallback=revision.fallback,
        warning_codes=revision.warning_codes_json,
        segments=[
            TranscriptSegmentPublic(
                id=item.id,
                ordinal=item.ordinal,
                text=field_codec.decrypt_text(
                    item.clinic_id,
                    "transcript_segment.text",
                    item.id,
                    item.text_ciphertext,
                ),
                text_start=item.text_start,
                text_end=item.text_end,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                speaker_id=item.speaker_id,
                detected_language=item.detected_language,
                confidence=item.confidence,
                confidence_source=item.confidence_source,
                overlap_group_id=item.overlap_group_id,
                provider=item.provider,
                model=item.model,
            )
            for item in segments
        ],
        facts=[_clinical_fact_public(item, stale=stale) for item in facts],
        created_at=revision.created_at,
    )


def current_transcript(
    db: Session, context: RequestContext, voice_session: VoiceSession
) -> TranscriptRevisionPublic:
    if context.role not in _CLINICAL_ROLES:
        raise HTTPException(
            status_code=403, detail="Clinical transcript access required"
        )
    if voice_session.current_transcript_revision_id is None:
        raise HTTPException(status_code=404, detail="Transcript is not available")
    revision = db.exec(
        select(TranscriptRevision).where(
            TranscriptRevision.clinic_id == context.clinic_id,
            TranscriptRevision.id == voice_session.current_transcript_revision_id,
        )
    ).first()
    if revision is None:
        raise HTTPException(status_code=404, detail="Transcript is not available")
    return transcript_public(db, voice_session, revision)


def correct_transcript(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    body: TranscriptCorrection,
) -> TranscriptRevision:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician review required")
    if voice_session.state not in {"ready", "needs_review"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_REVIEW_STATE_CONFLICT",
                "state": voice_session.state,
            },
        )
    if voice_session.published_entry_id is not None:
        raise HTTPException(status_code=409, detail={"code": "VOICE_ALREADY_PUBLISHED"})
    current_revision_id = _require_current_revision(
        voice_session, body.expected_revision_id
    )
    current = db.get(TranscriptRevision, current_revision_id)
    asset = db.exec(
        select(AudioAsset).where(
            AudioAsset.clinic_id == context.clinic_id,
            AudioAsset.session_id == voice_session.id,
        )
    ).first()
    if current is None or asset is None:
        raise HTTPException(status_code=409, detail={"code": "TRANSCRIPT_NOT_READY"})
    normalized = body.text.strip()
    if not normalized:
        raise HTTPException(
            status_code=422, detail={"code": "TRANSCRIPT_CORRECTION_EMPTY"}
        )
    revision_id = uuid.uuid4()
    revision = TranscriptRevision(
        id=revision_id,
        clinic_id=context.clinic_id,
        session_id=voice_session.id,
        revision_no=current.revision_no + 1,
        previous_revision_id=current.id,
        text_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "transcript_revision.text", revision_id, normalized
        ),
        text_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
        provider="human-correction",
        model="clinician-review-v1",
        detected_language="reviewed",
        status="needs_review",
        needs_review=True,
        stale=True,
        corrected_by_id=context.user_id,
        warning_codes_json=["DOWNSTREAM_RESULTS_STALE"],
    )
    segment_id = uuid.uuid4()
    segment = TranscriptSegment(
        id=segment_id,
        clinic_id=context.clinic_id,
        session_id=voice_session.id,
        revision_id=revision_id,
        ordinal=0,
        text_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "transcript_segment.text", segment_id, normalized
        ),
        text_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
        text_start=0,
        text_end=len(normalized),
        start_ms=0,
        end_ms=max(1, asset.duration_ms),
        speaker_id=None,
        detected_language="reviewed",
        confidence=None,
        confidence_source="human_correction",
        overlap_group_id=None,
        provider="human-correction",
        model="clinician-review-v1",
    )
    db.add(revision)
    db.add(segment)
    db.flush()
    voice_session.current_transcript_revision_id = revision.id
    voice_session.patient_summary_ciphertext = None
    voice_session.state = "needs_review"
    voice_session.warning_codes_json = ["DOWNSTREAM_RESULTS_STALE"]
    voice_session.updated_at = get_datetime_utc()
    db.add(voice_session)
    emit_change(
        db,
        context,
        action="voice.transcript_corrected",
        resource_type="transcript_revision",
        resource_id=revision.id,
        metadata={"session_id": str(voice_session.id), "state": "needs_review"},
    )
    return revision


def enqueue_reanalysis(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    *,
    expected_revision_id: uuid.UUID,
    idempotency_key: str,
) -> VoiceReanalyzePublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician review required")
    job, replayed = create_or_replay_job(
        db,
        context,
        patient_id=voice_session.patient_id,
        kind="voice_reanalyze",
        idempotency_key=idempotency_key,
        payload={
            "session_id": str(voice_session.id),
            "revision_id": str(expected_revision_id),
        },
    )
    # An idempotency replay returns the original durable operation even after
    # its output became the current revision. This covers a lost 202 response
    # without binding a second job to the newly derived revision.
    if replayed:
        if job.state == "failed":
            raise HTTPException(
                status_code=409, detail={"code": "VOICE_REANALYSIS_RETRY_REQUIRED"}
            )
        return VoiceReanalyzePublic(
            session_id=voice_session.id, job_id=job.id, state=voice_session.state
        )
    _require_current_revision(voice_session, expected_revision_id)
    if voice_session.state not in {"ready", "needs_review", "extracting"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_REVIEW_STATE_CONFLICT",
                "state": voice_session.state,
            },
        )
    if voice_session.published_entry_id is not None:
        raise HTTPException(status_code=409, detail={"code": "VOICE_ALREADY_PUBLISHED"})
    if voice_session.state == "extracting":
        if voice_session.processing_job_id != job.id:
            raise HTTPException(
                status_code=409, detail={"code": "VOICE_REANALYSIS_IN_PROGRESS"}
            )
        return VoiceReanalyzePublic(
            session_id=voice_session.id, job_id=job.id, state=voice_session.state
        )
    voice_session.processing_job_id = job.id
    voice_session.state = "extracting"
    voice_session.updated_at = get_datetime_utc()
    db.add(voice_session)
    return VoiceReanalyzePublic(
        session_id=voice_session.id, job_id=job.id, state=voice_session.state
    )


def _fact_evidence_is_current(
    db: Session,
    revision: TranscriptRevision,
    fact: ClinicalFact,
    text: str,
) -> bool:
    segment = db.get(TranscriptSegment, fact.segment_id)
    asset = db.get(AudioAsset, fact.audio_asset_id)
    quote = field_codec.decrypt_text(
        fact.clinic_id,
        "clinical_fact.exact_quote",
        fact.id,
        fact.exact_quote_ciphertext,
    )
    return bool(
        segment
        and asset
        and segment.revision_id == revision.id
        and asset.session_id == revision.session_id
        and validate_fact_evidence(
            transcript=text,
            transcript_start=fact.transcript_start,
            transcript_end=fact.transcript_end,
            exact_quote=quote,
            quote_sha256=fact.quote_sha256,
            segment_start_ms=segment.start_ms,
            segment_end_ms=segment.end_ms,
            audio_start_ms=fact.audio_start_ms,
            audio_end_ms=fact.audio_end_ms,
            asset_duration_ms=asset.duration_ms,
        )
    )


def publish_voice_result(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    *,
    expected_revision_id: uuid.UUID,
) -> VoicePublishPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician publication required")
    current_revision_id = _require_current_revision(voice_session, expected_revision_id)
    if voice_session.state == "published":
        if voice_session.published_entry_id is None:
            raise HTTPException(status_code=409, detail={"code": "PUBLICATION_INVALID"})
        entry = db.get(Entry, voice_session.published_entry_id)
        if entry is None or entry.current_version_id is None:
            raise HTTPException(status_code=409, detail={"code": "PUBLICATION_INVALID"})
        return VoicePublishPublic(
            session_id=voice_session.id,
            entry_id=entry.id,
            entry_version_id=entry.current_version_id,
        )
    if voice_session.published_entry_id is not None:
        raise HTTPException(status_code=409, detail={"code": "PUBLICATION_INVALID"})
    if voice_session.state not in {"ready", "needs_review"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "VOICE_NOT_PUBLISHABLE", "state": voice_session.state},
        )
    revision = db.get(TranscriptRevision, current_revision_id)
    if revision is None or revision.stale:
        raise HTTPException(
            status_code=409, detail={"code": "DOWNSTREAM_RESULTS_STALE"}
        )
    text = field_codec.decrypt_text(
        revision.clinic_id,
        "transcript_revision.text",
        revision.id,
        revision.text_ciphertext,
    )
    facts = db.exec(
        select(ClinicalFact)
        .where(
            ClinicalFact.clinic_id == context.clinic_id,
            ClinicalFact.revision_id == revision.id,
        )
        .order_by(col(ClinicalFact.ordinal))
    ).all()
    valid_facts = [
        fact
        for fact in facts
        if not fact.stale
        and fact.status in {"proposed", "accepted"}
        and _fact_evidence_is_current(db, revision, fact, text)
    ]
    if not valid_facts:
        raise HTTPException(status_code=409, detail={"code": "FACT_EVIDENCE_REQUIRED"})
    summary = (
        field_codec.decrypt_text(
            revision.clinic_id,
            "transcript_revision.summary",
            revision.id,
            revision.summary_ciphertext,
        )
        if revision.summary_ciphertext is not None
        else "Clinician-reviewed voice result"
    )
    evidence_quotes = [
        field_codec.decrypt_text(
            fact.clinic_id,
            "clinical_fact.exact_quote",
            fact.id,
            fact.exact_quote_ciphertext,
        )
        for fact in valid_facts
    ]
    reviewed_at = get_datetime_utc()
    for fact in valid_facts:
        fact.status = "accepted"
        fact.reviewed_by_id = context.user_id
        fact.reviewed_at = reviewed_at
        db.add(fact)
    content = summary
    source_entry_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    source_entry = Entry(
        id=source_entry_id,
        clinic_id=context.clinic_id,
        patient_id=voice_session.patient_id,
        section="system",
        origin="system",
        entry_type="voice_transcript_source",
        patient_facing=False,
        source_job_id=voice_session.processing_job_id,
    )
    source_version = EntryVersion(
        id=source_version_id,
        clinic_id=context.clinic_id,
        entry_id=source_entry_id,
        version_no=1,
        title_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "entry_version.title",
            source_version_id,
            "Voice transcript source",
        ),
        content_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "entry_version.content", source_version_id, text
        ),
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        patient_facing=False,
        author_id=context.user_id,
    )
    entry_id = uuid.uuid4()
    version_id = uuid.uuid4()
    entry = Entry(
        id=entry_id,
        clinic_id=context.clinic_id,
        patient_id=voice_session.patient_id,
        section="system",
        origin="ai",
        entry_type="voice_reviewed_result",
        patient_facing=False,
        source_job_id=voice_session.processing_job_id,
    )
    version = EntryVersion(
        id=version_id,
        clinic_id=context.clinic_id,
        entry_id=entry_id,
        version_no=1,
        title_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "entry_version.title",
            version_id,
            "Reviewed voice result",
        ),
        content_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "entry_version.content", version_id, content
        ),
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        patient_facing=False,
        author_id=context.user_id,
    )
    db.add(source_entry)
    db.add(source_version)
    db.add(entry)
    db.add(version)
    db.flush()
    source_entry.current_version_id = source_version.id
    entry.current_version_id = version.id
    db.add(source_entry)
    db.add(entry)
    # Link the published derivative to an immutable encrypted transcript source
    # without overwriting any prior care-note entry.
    db.add(
        EntryRelation(
            clinic_id=context.clinic_id,
            source_entry_id=entry.id,
            target_entry_id=source_entry.id,
            relation_type="derived_from_voice_transcript",
            created_by_id=context.user_id,
        )
    )
    for fact, quote in zip(valid_facts, evidence_quotes, strict=True):
        start = fact.transcript_start
        end = fact.transcript_end
        pointer_id = uuid.uuid4()
        db.add(
            ProvenancePointer(
                id=pointer_id,
                clinic_id=context.clinic_id,
                clinical_fact_id=fact.id,
                entry_version_id=source_version.id,
                start_offset=start,
                end_offset=end,
                exact_quote_ciphertext=field_codec.encrypt_text(
                    context.clinic_id, "provenance.exact_quote", pointer_id, quote
                ),
                prefix_ciphertext=field_codec.encrypt_text(
                    context.clinic_id,
                    "provenance.prefix",
                    pointer_id,
                    text[max(0, start - 32) : start],
                ),
                suffix_ciphertext=field_codec.encrypt_text(
                    context.clinic_id,
                    "provenance.suffix",
                    pointer_id,
                    text[end : end + 32],
                ),
                quote_sha256=fact.quote_sha256,
                audio_asset_id=fact.audio_asset_id,
                audio_start_ms=fact.audio_start_ms,
                audio_end_ms=fact.audio_end_ms,
            )
        )
    voice_session.published_entry_id = entry.id
    voice_session.patient_summary_ciphertext = field_codec.encrypt_text(
        context.clinic_id,
        "voice_session.patient_summary",
        voice_session.id,
        summary,
    )
    voice_session.state = "published"
    voice_session.updated_at = get_datetime_utc()
    db.add(voice_session)
    emit_change(
        db,
        context,
        action="voice.published",
        resource_type="voice_session",
        resource_id=voice_session.id,
        metadata={
            "entry_id": str(entry.id),
            "revision_id": str(revision.id),
            "accepted_fact_ids": [str(fact.id) for fact in valid_facts],
        },
    )
    return VoicePublishPublic(
        session_id=voice_session.id,
        entry_id=entry.id,
        entry_version_id=version.id,
    )


def authorized_audio_asset(
    db: Session, context: RequestContext, voice_session: VoiceSession
) -> tuple[bytes, str]:
    # Patient-facing routes expose only derived status/summary DTOs.  Raw
    # normalized audio remains a clinical review artifact even when the
    # patient recorded the source capture.
    if context.role not in _CLINICAL_ROLES:
        raise HTTPException(status_code=403, detail="Audio access is not permitted")
    asset = db.exec(
        select(AudioAsset).where(
            AudioAsset.clinic_id == context.clinic_id,
            AudioAsset.session_id == voice_session.id,
        )
    ).first()
    if asset is None:
        raise HTTPException(status_code=404, detail="Audio asset is not available")
    payload = field_codec.decrypt(
        asset.clinic_id, "audio_asset.payload", asset.id, asset.payload_ciphertext
    )
    if hashlib.sha256(payload).hexdigest() != asset.plaintext_sha256:
        raise HTTPException(
            status_code=409, detail={"code": "AUDIO_ASSET_HASH_INVALID"}
        )
    return payload, asset.media_type


def trusted_device_ids(body: VoiceFinalizeRequest) -> Iterable[uuid.UUID]:
    return (item.device_id for item in body.devices)
