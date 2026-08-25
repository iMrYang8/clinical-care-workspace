from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    AudioAsset,
    AudioChunk,
    ClinicalFact,
    Job,
    JobAttempt,
    TranscriptRevision,
    TranscriptSegment,
    VoiceDevice,
    VoiceSession,
    get_datetime_utc,
)
from app.services.ai_jobs import claim_job
from app.services.nightingale import emit_change
from app.services.voice.ffmpeg import (
    AudioPreprocessingError,
    DeviceAudio,
    preprocess_audio,
    write_private_file,
)
from app.services.voice.provenance import validate_fact_evidence
from app.services.voice.providers.base import (
    TranscriptionProvider,
    TranscriptResult,
    TranscriptSegmentResult,
    validate_transcript_result,
)
from app.services.voice.providers.deterministic import SyntheticFixtureProvider
from app.services.voice.providers.local_whisper import LocalFasterWhisperProvider
from app.services.voice.providers.openai_audio import OpenAIAudioTranscriptionProvider


class VoiceJobError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _payload(context: RequestContext, job: Job) -> dict[str, Any]:
    payload = field_codec.decrypt_json(
        context.clinic_id, "job.payload", job.id, job.payload_ciphertext
    )
    if not isinstance(payload, dict):
        raise VoiceJobError("INVALID_VOICE_JOB_PAYLOAD")
    return payload


def _session_from_payload(
    db: Session, context: RequestContext, job: Job, payload: dict[str, Any]
) -> VoiceSession:
    try:
        session_id = uuid.UUID(str(payload["session_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise VoiceJobError("INVALID_VOICE_SESSION_ID") from exc
    voice_session = db.exec(
        select(VoiceSession)
        .where(
            VoiceSession.clinic_id == context.clinic_id,
            VoiceSession.id == session_id,
            VoiceSession.patient_id == job.patient_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if voice_session is None or voice_session.processing_job_id != job.id:
        raise VoiceJobError("VOICE_JOB_BINDING_INVALID")
    return voice_session


def _reanalyze_revision_from_payload(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    payload: dict[str, Any],
) -> TranscriptRevision:
    try:
        revision_id = uuid.UUID(str(payload["revision_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise VoiceJobError("INVALID_REANALYSIS_REVISION_ID") from exc
    revision = db.exec(
        select(TranscriptRevision).where(
            TranscriptRevision.clinic_id == context.clinic_id,
            TranscriptRevision.session_id == voice_session.id,
            TranscriptRevision.id == revision_id,
        )
    ).first()
    if revision is None:
        raise VoiceJobError("REANALYSIS_REVISION_NOT_FOUND")
    if voice_session.current_transcript_revision_id != revision.id:
        # A queued extraction is bound to one immutable transcript.  Never
        # silently switch it to a correction that appeared later.
        raise VoiceJobError("REANALYSIS_REVISION_STALE")
    return revision


def _claim_is_current(
    db: Session, context: RequestContext, job_id: uuid.UUID, token: uuid.UUID
) -> tuple[Job, JobAttempt]:
    job = db.exec(
        select(Job)
        .where(Job.clinic_id == context.clinic_id, Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    attempt = db.exec(
        select(JobAttempt)
        .where(
            JobAttempt.clinic_id == context.clinic_id,
            JobAttempt.id == token,
            JobAttempt.job_id == job_id,
            JobAttempt.worker_membership_id == context.membership.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    now = get_datetime_utc()
    if (
        job is None
        or attempt is None
        or job.state != "running"
        or job.locked_by != str(token)
        or job.locked_until is None
        or job.locked_until <= now
        or attempt.status != "started"
        or not context.membership.is_active
        or not context.user.is_active
    ):
        raise VoiceJobError("JOB_CLAIM_LOST")
    return job, attempt


def _renew_claim(
    db: Session, context: RequestContext, job_id: uuid.UUID, token: uuid.UUID
) -> tuple[Job, JobAttempt]:
    job, attempt = _claim_is_current(db, context, job_id, token)
    job.locked_until = get_datetime_utc() + timedelta(
        seconds=max(30, settings.VOICE_JOB_LEASE_SECONDS)
    )
    job.updated_at = get_datetime_utc()
    db.add(job)
    db.commit()
    return _claim_is_current(db, context, job_id, token)


def _transition_state(
    db: Session,
    context: RequestContext,
    session_id: uuid.UUID,
    *,
    expected: set[str],
    target: str,
) -> VoiceSession:
    voice_session = db.exec(
        select(VoiceSession)
        .where(
            VoiceSession.clinic_id == context.clinic_id,
            VoiceSession.id == session_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if voice_session.state == target:
        return voice_session
    if voice_session.state not in expected:
        raise VoiceJobError("VOICE_STATE_TRANSITION_CONFLICT")
    voice_session.state = target
    voice_session.updated_at = get_datetime_utc()
    db.add(voice_session)
    db.flush()
    return voice_session


def _device_audio(
    db: Session, context: RequestContext, voice_session: VoiceSession
) -> list[DeviceAudio]:
    devices = db.exec(
        select(VoiceDevice)
        .where(
            VoiceDevice.clinic_id == context.clinic_id,
            VoiceDevice.session_id == voice_session.id,
            col(VoiceDevice.last_declared_chunk_index).is_not(None),
        )
        .order_by(col(VoiceDevice.created_at), col(VoiceDevice.id))
    ).all()
    output: list[DeviceAudio] = []
    for device in devices:
        chunks = db.exec(
            select(AudioChunk)
            .where(
                AudioChunk.clinic_id == context.clinic_id,
                AudioChunk.session_id == voice_session.id,
                AudioChunk.device_id == device.id,
            )
            .order_by(col(AudioChunk.chunk_index))
        ).all()
        if device.last_declared_chunk_index is None or len(chunks) != (
            device.last_declared_chunk_index + 1
        ):
            raise VoiceJobError("MISSING_AUDIO_CHUNKS")
        decoded: list[tuple[int, bytes, str]] = []
        for chunk in chunks:
            payload = field_codec.decrypt(
                chunk.clinic_id,
                "audio_chunk.payload",
                chunk.id,
                chunk.payload_ciphertext,
            )
            decoded.append((chunk.chunk_index, payload, chunk.plaintext_sha256))
        output.append(
            DeviceAudio(
                device_id=str(device.id),
                media_type=chunks[0].media_type,
                chunks=decoded,
            )
        )
    if not output:
        raise VoiceJobError("NO_AUDIO_CHUNKS")
    return output


def _store_asset(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
) -> AudioAsset:
    existing = db.exec(
        select(AudioAsset).where(
            AudioAsset.clinic_id == context.clinic_id,
            AudioAsset.session_id == voice_session.id,
        )
    ).first()
    if existing is not None:
        return existing
    processed = preprocess_audio(
        _device_audio(db, context, voice_session),
        ffmpeg_bin=settings.VOICE_FFMPEG_BIN,
        timeout_seconds=settings.VOICE_FFMPEG_TIMEOUT_SECONDS,
    )
    asset_id = uuid.uuid4()
    asset = AudioAsset(
        id=asset_id,
        clinic_id=context.clinic_id,
        session_id=voice_session.id,
        payload_ciphertext=field_codec.encrypt(
            context.clinic_id, "audio_asset.payload", asset_id, processed.payload
        ),
        plaintext_sha256=processed.sha256,
        duration_ms=processed.duration_ms,
        media_type="audio/wav",
        sample_rate_hz=processed.sample_rate_hz,
        channels=processed.channels,
        preprocessing_json=processed.signals,
    )
    db.add(asset)
    db.flush()
    return asset


def _configured_provider(
    voice_session: VoiceSession,
) -> tuple[TranscriptionProvider | None, str | None]:
    if voice_session.synthetic_fixture:
        return None, None
    if settings.VOICE_TRANSCRIPTION_PROVIDER == "disabled":
        return None, "ASR_PROVIDER_DISABLED"
    if settings.VOICE_TRANSCRIPTION_PROVIDER == "openai":
        if settings.STRICT_NO_AUDIO_EGRESS:
            return None, "STRICT_NO_AUDIO_EGRESS"
        if not settings.REMOTE_AUDIO_EGRESS_ENABLED:
            return None, "REMOTE_AUDIO_EGRESS_DISABLED"
        if not settings.OPENAI_API_KEY or not settings.OPENAI_TRANSCRIBE_MODEL:
            return None, "OPENAI_AUDIO_NOT_CONFIGURED"
        return (
            OpenAIAudioTranscriptionProvider(
                api_key=settings.OPENAI_API_KEY,
                model=settings.OPENAI_TRANSCRIBE_MODEL,
            ),
            None,
        )
    if settings.VOICE_TRANSCRIPTION_PROVIDER == "local":
        if not settings.LOCAL_ASR_MODEL_DIR:
            return None, "LOCAL_ASR_MODEL_REQUIRED"
        try:
            return LocalFasterWhisperProvider(settings.LOCAL_ASR_MODEL_DIR), None
        except ValueError:
            return None, "LOCAL_ASR_MODEL_NOT_CACHED"
    return None, "ASR_PROVIDER_UNAVAILABLE"


async def _transcribe(
    voice_session: VoiceSession, asset_payload: bytes
) -> tuple[TranscriptResult | None, str | None]:
    if voice_session.synthetic_fixture:
        if not voice_session.fixture_id:
            return None, "SYNTHETIC_FIXTURE_ID_MISSING"
        return (
            SyntheticFixtureProvider().transcribe_fixture(voice_session.fixture_id),
            None,
        )
    provider, reason = _configured_provider(voice_session)
    if provider is None:
        return None, reason
    with tempfile.TemporaryDirectory(prefix="nightingale-asr-") as temp_name:
        temp_dir = Path(temp_name)
        os.chmod(temp_dir, stat.S_IRWXU)
        audio_path = temp_dir / "normalized.wav"
        write_private_file(audio_path, asset_payload)
        try:
            result = await asyncio.wait_for(
                provider.transcribe(audio_path),
                timeout=max(1, settings.VOICE_ASR_TIMEOUT_SECONDS),
            )
        except TimeoutError:
            return None, "ASR_TIMEOUT"
        return validate_transcript_result(result), None


def _normalized_segments(
    result: TranscriptResult, asset: AudioAsset
) -> tuple[list[TranscriptSegmentResult], list[str]]:
    cursor = 0
    output: list[TranscriptSegmentResult] = []
    warnings: list[str] = []
    for segment in result.segments:
        start = segment.text_start
        end = segment.text_end
        if start is None or end is None:
            start = result.text.find(segment.text, cursor)
            if start < 0:
                warnings.append("INVALID_TRANSCRIPT_SEGMENT_SPAN")
                continue
            end = start + len(segment.text)
        cursor = end
        # Never manufacture audio provenance by clamping provider timestamps.
        # An out-of-range segment is omitted, forcing any fact that depended on
        # it to be discarded and the revision/session into clinical review.
        if segment.start_ms >= asset.duration_ms or segment.end_ms > asset.duration_ms:
            warnings.append("SEGMENT_TIME_OUT_OF_BOUNDS")
            continue
        output.append(
            replace(
                segment,
                text_start=start,
                text_end=end,
            )
        )
    if not output:
        raise VoiceJobError("NO_VALID_TRANSCRIPT_SEGMENTS")
    return output, warnings


def _fact_candidate(
    text: str, segments: list[TranscriptSegmentResult]
) -> tuple[str, str, int, int, TranscriptSegmentResult] | None:
    lowered = text.lower()
    for phrase, value in (
        ("penicillin allergy", "penicillin allergy"),
        ("allergy to penicillin", "penicillin allergy"),
    ):
        start = lowered.find(phrase)
        if start < 0:
            continue
        end = start + len(phrase)
        for segment in segments:
            if (
                segment.text_start is not None
                and segment.text_end is not None
                and segment.text_start <= start
                and segment.text_end >= end
            ):
                return text[start:end], value, start, end, segment
    return None


def _create_revision(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    asset: AudioAsset,
    result: TranscriptResult,
    *,
    previous_revision: TranscriptRevision | None = None,
    reanalyzed: bool = False,
) -> TranscriptRevision:
    segments, span_warnings = _normalized_segments(result, asset)
    fact_candidate = _fact_candidate(result.text, segments)
    fact_valid = False
    if fact_candidate is not None:
        quote, _value, start, end, source_segment = fact_candidate
        fact_valid = validate_fact_evidence(
            transcript=result.text,
            transcript_start=start,
            transcript_end=end,
            exact_quote=quote,
            quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
            segment_start_ms=source_segment.start_ms,
            segment_end_ms=source_segment.end_ms,
            audio_start_ms=source_segment.start_ms,
            audio_end_ms=source_segment.end_ms,
            asset_duration_ms=asset.duration_ms,
        )
    preprocessing_warning_map = {
        "silence_review": "SILENCE_REVIEW",
        "clipping_review": "CLIPPING_REVIEW",
        "noise_review": "NOISE_REVIEW",
        "overlap_review": "MULTI_DEVICE_OVERLAP_REVIEW",
    }
    preprocessing_warnings = {
        warning
        for signal, warning in preprocessing_warning_map.items()
        if asset.preprocessing_json.get(signal) is True
    }
    warnings = sorted({*result.warnings, *span_warnings, *preprocessing_warnings})
    low_confidence = any(
        item.confidence is not None and item.confidence < 0.75 for item in segments
    )
    confidence_unavailable = any(item.confidence is None for item in segments)
    overlap = any(item.overlap_group_id for item in segments)
    if low_confidence:
        warnings.append("LOW_CONFIDENCE_REVIEW")
    if confidence_unavailable:
        warnings.append("CONFIDENCE_UNAVAILABLE")
    if overlap:
        warnings.append("OVERLAP_REVIEW")
    if fact_candidate is None:
        warnings.append("NO_STRUCTURED_FACTS")
    elif not fact_valid:
        warnings.append("INVALID_FACT_EVIDENCE")
    warnings = sorted(set(warnings))
    # Provider and preprocessing warnings are review signals.  A clinician can
    # still explicitly publish evidence-backed facts, but the UI never labels
    # these outputs as automatically ready.
    needs_review = (
        low_confidence
        or confidence_unavailable
        or overlap
        or not fact_valid
        or bool(warnings)
    )
    summary = (
        "Recorded allergy information is ready for clinician review."
        if fact_candidate is not None
        else "Clinical recording processed; structured facts require clinician review."
    )
    revision_id = uuid.uuid4()
    prior_no = previous_revision.revision_no if previous_revision else 0
    revision = TranscriptRevision(
        id=revision_id,
        clinic_id=context.clinic_id,
        session_id=voice_session.id,
        revision_no=prior_no + 1,
        previous_revision_id=previous_revision.id if previous_revision else None,
        text_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "transcript_revision.text", revision_id, result.text
        ),
        text_sha256=hashlib.sha256(result.text.encode()).hexdigest(),
        summary_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "transcript_revision.summary", revision_id, summary
        ),
        provider=result.provider,
        model=result.model,
        detected_language=result.detected_language,
        status="needs_review" if needs_review else "ready",
        needs_review=needs_review,
        stale=False,
        fallback=False,
        corrected_by_id=(
            previous_revision.corrected_by_id
            if reanalyzed and previous_revision is not None
            else None
        ),
        warning_codes_json=warnings,
    )
    db.add(revision)
    db.flush()
    persisted_segments: list[tuple[TranscriptSegment, TranscriptSegmentResult]] = []
    for ordinal, item in enumerate(segments):
        segment_id = uuid.uuid4()
        row = TranscriptSegment(
            id=segment_id,
            clinic_id=context.clinic_id,
            session_id=voice_session.id,
            revision_id=revision.id,
            ordinal=ordinal,
            text_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "transcript_segment.text", segment_id, item.text
            ),
            text_sha256=hashlib.sha256(item.text.encode()).hexdigest(),
            text_start=item.text_start or 0,
            text_end=item.text_end or len(item.text),
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            speaker_id=item.speaker_id,
            detected_language=item.detected_language,
            confidence=item.confidence,
            confidence_source=item.confidence_source,
            overlap_group_id=item.overlap_group_id,
            provider=result.provider,
            model=result.model,
        )
        db.add(row)
        persisted_segments.append((row, item))
    db.flush()
    if fact_candidate is not None:
        quote, value, start, end, source_segment = fact_candidate
        segment_row = next(
            row for row, candidate in persisted_segments if candidate is source_segment
        )
        fact_id = uuid.uuid4()
        quote_hash = hashlib.sha256(quote.encode()).hexdigest()
        valid = validate_fact_evidence(
            transcript=result.text,
            transcript_start=start,
            transcript_end=end,
            exact_quote=quote,
            quote_sha256=quote_hash,
            segment_start_ms=source_segment.start_ms,
            segment_end_ms=source_segment.end_ms,
            audio_start_ms=source_segment.start_ms,
            audio_end_ms=source_segment.end_ms,
            asset_duration_ms=asset.duration_ms,
        )
        if valid and fact_valid:
            db.add(
                ClinicalFact(
                    id=fact_id,
                    clinic_id=context.clinic_id,
                    session_id=voice_session.id,
                    revision_id=revision.id,
                    segment_id=segment_row.id,
                    ordinal=0,
                    fact_type="allergy",
                    value_ciphertext=field_codec.encrypt_text(
                        context.clinic_id, "clinical_fact.value", fact_id, value
                    ),
                    exact_quote_ciphertext=field_codec.encrypt_text(
                        context.clinic_id,
                        "clinical_fact.exact_quote",
                        fact_id,
                        quote,
                    ),
                    quote_sha256=quote_hash,
                    transcript_start=start,
                    transcript_end=end,
                    audio_asset_id=asset.id,
                    audio_start_ms=source_segment.start_ms,
                    audio_end_ms=source_segment.end_ms,
                    status="proposed",
                    patient_facing=True,
                )
            )
    db.flush()
    return revision


def _complete_attempt(
    db: Session,
    context: RequestContext,
    job: Job,
    attempt: JobAttempt,
    voice_session: VoiceSession,
    *,
    needs_review: bool,
) -> Job:
    target_state = "needs_review" if needs_review else "ready"
    if voice_session.state != target_state and voice_session.state not in {
        "transcribing",
        "redacting",
        "extracting",
    }:
        raise VoiceJobError("VOICE_STATE_TRANSITION_CONFLICT")
    attempt.status = "completed"
    attempt.completed_at = get_datetime_utc()
    job.state = "needs_review" if needs_review else "completed"
    job.locked_by = None
    job.locked_until = None
    job.error_code = None
    job.updated_at = get_datetime_utc()
    voice_session.state = target_state
    if not needs_review:
        voice_session.error_code = None
    voice_session.updated_at = get_datetime_utc()
    db.add(attempt)
    db.add(job)
    db.add(voice_session)
    emit_change(
        db,
        context,
        action="voice.processing_completed",
        resource_type="voice_session",
        resource_id=voice_session.id,
        metadata={"job_id": str(job.id), "state": voice_session.state},
    )
    db.commit()
    return job


async def process_voice_job(
    db: Session, context: RequestContext, job_id: uuid.UUID
) -> Job:
    token = uuid.uuid4()
    job = claim_job(db, context, job_id, claim_token=token)
    if job.kind not in {"voice_process", "voice_reanalyze"}:
        raise HTTPException(status_code=409, detail={"code": "VOICE_JOB_KIND_INVALID"})
    attempt = JobAttempt(
        id=token,
        clinic_id=context.clinic_id,
        job_id=job.id,
        worker_membership_id=context.membership.id,
        attempt_no=job.attempt_count + 1,
    )
    job.attempt_count += 1
    db.add(job)
    db.add(attempt)
    db.flush()
    db.commit()
    voice_session: VoiceSession | None = None
    try:
        job, attempt = _claim_is_current(db, context, job_id, token)
        payload = _payload(context, job)
        voice_session = _session_from_payload(db, context, job, payload)
        asset = db.exec(
            select(AudioAsset).where(
                AudioAsset.clinic_id == context.clinic_id,
                AudioAsset.session_id == voice_session.id,
            )
        ).first()
        if job.kind == "voice_process":
            existing_revision = (
                db.get(TranscriptRevision, voice_session.current_transcript_revision_id)
                if voice_session.current_transcript_revision_id
                else None
            )
            if existing_revision is not None:
                return _complete_attempt(
                    db,
                    context,
                    job,
                    attempt,
                    voice_session,
                    needs_review=existing_revision.needs_review,
                )
            if voice_session.state == "needs_review" and job.attempt_count > 1:
                # An explicit retry may resume a transient preprocessing/ASR
                # failure. The immutable asset decides the earliest safe stage.
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"needs_review"},
                    target="transcribing" if asset is not None else "preprocessing",
                )
                db.commit()
                job, attempt = _claim_is_current(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
            resumable_states = {
                "finalizing",
                "assembling",
                "preprocessing",
                "transcribing",
                "redacting",
                "extracting",
            }
            if voice_session.state not in resumable_states:
                raise VoiceJobError("VOICE_STATE_TRANSITION_CONFLICT")
            if voice_session.state == "finalizing":
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"finalizing"},
                    target="assembling",
                )
                db.commit()
                job, attempt = _claim_is_current(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
            if voice_session.state == "assembling":
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"assembling"},
                    target="preprocessing",
                )
                db.commit()
                job, attempt = _claim_is_current(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
            if asset is None:
                if voice_session.state != "preprocessing":
                    raise VoiceJobError("VOICE_AUDIO_ASSET_MISSING")
                job, attempt = _renew_claim(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
                asset = _store_asset(db, context, voice_session)
                db.commit()
                job, attempt = _claim_is_current(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
            if voice_session.state == "preprocessing":
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"preprocessing"},
                    target="transcribing",
                )
                db.commit()
                job, attempt = _claim_is_current(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
            if voice_session.state not in {"transcribing", "redacting", "extracting"}:
                raise VoiceJobError("VOICE_STATE_TRANSITION_CONFLICT")
            asset_payload = field_codec.decrypt(
                asset.clinic_id,
                "audio_asset.payload",
                asset.id,
                asset.payload_ciphertext,
            )
            job, attempt = _renew_claim(db, context, job_id, token)
            voice_session = _session_from_payload(db, context, job, payload)
            result, unavailable_reason = await _transcribe(voice_session, asset_payload)
            job, attempt = _claim_is_current(db, context, job_id, token)
            voice_session = _session_from_payload(db, context, job, payload)
            if result is None:
                voice_session.error_code = (
                    unavailable_reason or "ASR_PROVIDER_UNAVAILABLE"
                )
                voice_session.warning_codes_json = ["TRANSCRIPT_PENDING"]
                voice_session.updated_at = get_datetime_utc()
                db.add(voice_session)
                return _complete_attempt(
                    db,
                    context,
                    job,
                    attempt,
                    voice_session,
                    needs_review=True,
                )
            if voice_session.state == "transcribing":
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"transcribing"},
                    target="redacting",
                )
                db.commit()
                job, attempt = _claim_is_current(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
            if voice_session.state == "redacting":
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"redacting"},
                    target="extracting",
                )
            if voice_session.state != "extracting":
                raise VoiceJobError("VOICE_STATE_TRANSITION_CONFLICT")
            revision = _create_revision(db, context, voice_session, asset, result)
        else:
            previous = _reanalyze_revision_from_payload(
                db, context, voice_session, payload
            )
            if voice_session.state == "needs_review" and job.attempt_count > 1:
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"needs_review"},
                    target="extracting",
                )
            if voice_session.state != "extracting":
                raise VoiceJobError("VOICE_STATE_TRANSITION_CONFLICT")
            if asset is None:
                raise VoiceJobError("TRANSCRIPT_NOT_READY")
            text = field_codec.decrypt_text(
                previous.clinic_id,
                "transcript_revision.text",
                previous.id,
                previous.text_ciphertext,
            )
            previous_segments = db.exec(
                select(TranscriptSegment)
                .where(
                    TranscriptSegment.clinic_id == context.clinic_id,
                    TranscriptSegment.revision_id == previous.id,
                )
                .order_by(col(TranscriptSegment.ordinal))
            ).all()
            normalized = [
                TranscriptSegmentResult(
                    text=field_codec.decrypt_text(
                        row.clinic_id,
                        "transcript_segment.text",
                        row.id,
                        row.text_ciphertext,
                    ),
                    start_ms=row.start_ms,
                    end_ms=row.end_ms,
                    speaker_id=row.speaker_id,
                    detected_language=row.detected_language,
                    confidence=row.confidence,
                    confidence_source=row.confidence_source,
                    overlap_group_id=row.overlap_group_id,
                    text_start=row.text_start,
                    text_end=row.text_end,
                )
                for row in previous_segments
            ]
            result = TranscriptResult(
                text=text,
                segments=normalized,
                provider="deterministic-evidence-extractor",
                model="nightingale-voice-extractor-v1",
                detected_language=previous.detected_language,
                warnings=("CLINICIAN_CORRECTED_TRANSCRIPT",),
            )
            revision = _create_revision(
                db,
                context,
                voice_session,
                asset,
                result,
                previous_revision=previous,
                reanalyzed=True,
            )
        voice_session.current_transcript_revision_id = revision.id
        voice_session.warning_codes_json = revision.warning_codes_json
        voice_session.error_code = None
        db.add(voice_session)
        db.flush()
        return _complete_attempt(
            db,
            context,
            job,
            attempt,
            voice_session,
            needs_review=revision.needs_review,
        )
    except Exception as exc:
        db.rollback()
        try:
            job, attempt = _claim_is_current(db, context, job_id, token)
        except Exception:
            db.rollback()
            raise HTTPException(status_code=409, detail={"code": "JOB_CLAIM_LOST"})
        code = (
            exc.code
            if isinstance(exc, (VoiceJobError, AudioPreprocessingError))
            else "VOICE_JOB_FAILED"
        )
        attempt.status = "failed"
        attempt.error_code = code
        attempt.completed_at = get_datetime_utc()
        job.state = "failed"
        job.error_code = code
        job.locked_by = None
        job.locked_until = None
        job.updated_at = get_datetime_utc()
        db.add(attempt)
        db.add(job)
        if voice_session is not None:
            voice_session.state = "needs_review"
            voice_session.error_code = code
            voice_session.warning_codes_json = ["PROCESSING_FAILED"]
            voice_session.updated_at = get_datetime_utc()
            db.add(voice_session)
        db.commit()
        return job
