from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    AudioAsset,
    AudioChunk,
    CalibrationReport,
    ClinicalFact,
    Job,
    JobAttempt,
    ProviderCircuitState,
    TranscriptRevision,
    TranscriptSegment,
    VoiceDevice,
    VoiceSession,
    get_datetime_utc,
)
from app.services.ai_jobs import active_worker_for_context, claim_job
from app.services.clinic_ai_settings import clinic_ai_runtime
from app.services.conflicts import (
    NormalizedFact,
    detect_language_spans,
    extract_normalized_facts,
    normalize_language_code,
)
from app.services.decisioning import (
    evaluation_manifest_sha256,
    matching_calibration_report,
)
from app.services.nightingale import emit_change
from app.services.provider_resilience import (
    ProviderCircuitOpen,
    ProviderFailure,
    classify_provider_failure,
    retry_delay_seconds,
)
from app.services.voice.diarization import (
    LocalPyannoteDiarizer,
    apply_local_diarization,
    pyannote_runtime_status,
)
from app.services.voice.egress_policy import remote_audio_egress_denial
from app.services.voice.ffmpeg import (
    AudioPreprocessingError,
    DeviceAudio,
    preprocess_audio,
    write_private_file,
)
from app.services.voice.language import (
    CLINIC_CONFIGURABLE_LANGUAGE_CODES,
    AddressableLanguageSpan,
    LanguageCode,
    apply_clinic_language_policy,
    clinic_supported_language_codes,
    language_span_from_payload,
    language_span_payload,
    validate_addressable_language_spans,
)
from app.services.voice.multi_agent import (
    consult_agent_payload,
    consult_fact_candidates,
    consult_summary,
    consult_warning_codes,
    run_consult_on_segments,
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


_VOICE_PROCESSING_STATES = {
    "finalizing",
    "assembling",
    "preprocessing",
    "transcribing",
    "redacting",
    "extracting",
}
_AUDIO_PROVIDER = "openai"
_AUDIO_CAPABILITY = "audio_transcription"


def _audio_circuit(
    db: Session, clinic_id: uuid.UUID, *, lock: bool = False
) -> ProviderCircuitState | None:
    statement = select(ProviderCircuitState).where(
        ProviderCircuitState.clinic_id == clinic_id,
        ProviderCircuitState.provider == _AUDIO_PROVIDER,
        ProviderCircuitState.capability == _AUDIO_CAPABILITY,
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    return db.exec(statement).first()


def _assert_audio_circuit_available(db: Session, clinic_id: uuid.UUID) -> None:
    circuit = _audio_circuit(db, clinic_id, lock=True)
    if circuit is None or circuit.state == "closed":
        return
    now = get_datetime_utc()
    if circuit.next_probe_at is not None and circuit.next_probe_at > now:
        raise ProviderCircuitOpen("PROVIDER_CIRCUIT_OPEN")
    circuit.state = "half_open"
    circuit.updated_at = now
    db.add(circuit)
    db.flush()


def _record_audio_provider_failure(
    db: Session,
    job: Job,
    failure: ProviderFailure,
    *,
    attempt_no: int,
) -> tuple[datetime | None, ProviderCircuitState]:
    now = get_datetime_utc()
    retryable = failure.retryable and attempt_no < job.max_attempts
    next_retry = (
        now + timedelta(seconds=retry_delay_seconds(job.id, attempt_no))
        if retryable
        else None
    )
    circuit = _audio_circuit(db, job.clinic_id, lock=True)
    if circuit is None:
        candidate = ProviderCircuitState(
            clinic_id=job.clinic_id,
            provider=_AUDIO_PROVIDER,
            capability=_AUDIO_CAPABILITY,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            circuit = candidate
        except IntegrityError:
            circuit = _audio_circuit(db, job.clinic_id, lock=True)
            if circuit is None:
                raise
    circuit.state = "open" if failure.retryable else "closed"
    circuit.consecutive_failures += 1
    circuit.last_error_class = failure.failure_class
    circuit.opened_at = circuit.opened_at or now
    circuit.next_probe_at = (
        next_retry
        if next_retry is not None
        else now + timedelta(seconds=3_600)
        if failure.retryable
        else None
    )
    circuit.updated_at = now
    db.add(circuit)
    return next_retry, circuit


def _record_audio_provider_success(db: Session, job: Job) -> None:
    circuit = _audio_circuit(db, job.clinic_id, lock=True)
    if circuit is None:
        return
    now = get_datetime_utc()
    circuit.state = "closed"
    circuit.consecutive_failures = 0
    circuit.last_error_class = None
    circuit.opened_at = None
    circuit.next_probe_at = None
    circuit.last_success_at = now
    circuit.updated_at = now
    db.add(circuit)


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
        or context.job_id != job_id
        or active_worker_for_context(db, context, lock=True) is None
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
    # Do not immediately reacquire row locks after committing the renewed
    # lease.  The following ASR call may run for minutes; holding a shared lock
    # on the worker membership for that whole interval would prevent an admin
    # revocation from committing.  The caller fences every derived write with
    # a fresh `_claim_is_current` check after the external provider returns.
    return job, attempt


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
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    temp_dir: Path,
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
    session_bytes = 0
    for device_index, device in enumerate(devices):
        chunks = db.exec(
            select(AudioChunk)
            .where(
                AudioChunk.clinic_id == context.clinic_id,
                AudioChunk.session_id == voice_session.id,
                AudioChunk.device_id == device.id,
            )
            .order_by(col(AudioChunk.chunk_index))
            .execution_options(yield_per=1)
        )
        path = temp_dir / f"device-{device_index}.audio"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        expected_index = 0
        media_type: str | None = None
        with os.fdopen(descriptor, "wb") as assembled:
            for chunk in chunks:
                if chunk.chunk_index != expected_index:
                    raise VoiceJobError("AUDIO_CHUNK_SEQUENCE_INVALID")
                if media_type is None:
                    media_type = chunk.media_type
                elif chunk.media_type != media_type:
                    raise VoiceJobError("AUDIO_CHUNK_MEDIA_TYPE_MISMATCH")
                payload = field_codec.decrypt(
                    chunk.clinic_id,
                    "audio_chunk.payload",
                    chunk.id,
                    chunk.payload_ciphertext,
                )
                if hashlib.sha256(payload).hexdigest() != chunk.plaintext_sha256:
                    raise VoiceJobError("AUDIO_CHUNK_HASH_INVALID")
                session_bytes += len(payload)
                if session_bytes > settings.VOICE_MAX_SESSION_BYTES:
                    raise VoiceJobError("VOICE_SESSION_SIZE_LIMIT_REACHED")
                assembled.write(payload)
                expected_index += 1
                db.expunge(chunk)
        if (
            device.last_declared_chunk_index is None
            or expected_index != device.last_declared_chunk_index + 1
        ):
            raise VoiceJobError("MISSING_AUDIO_CHUNKS")
        if media_type is None:
            raise VoiceJobError("NO_AUDIO_CHUNKS")
        output.append(
            DeviceAudio(
                device_id=str(device.id),
                media_type=media_type,
                chunks=[],
                source_path=path,
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
    with tempfile.TemporaryDirectory(
        prefix="nightingale-assembled-audio-"
    ) as temp_name:
        temp_dir = Path(temp_name)
        os.chmod(temp_dir, stat.S_IRWXU)
        processed = preprocess_audio(
            _device_audio(db, context, voice_session, temp_dir),
            ffmpeg_bin=settings.VOICE_FFMPEG_BIN,
            timeout_seconds=settings.VOICE_FFMPEG_TIMEOUT_SECONDS,
            max_duration_ms=settings.VOICE_MAX_DECODED_DURATION_MS,
            max_output_bytes=settings.VOICE_MAX_NORMALIZED_BYTES,
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
    db: Session,
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
        policy_denial = remote_audio_egress_denial(db, voice_session)
        if policy_denial is not None:
            return None, policy_denial
        # Load clinic credentials only after both policy and consent pass. A
        # denied PHI egress path must not touch the provider secret boundary.
        runtime = clinic_ai_runtime(db, voice_session.clinic_id)
        if not runtime.api_key or not runtime.transcribe_model:
            return None, "OPENAI_AUDIO_NOT_CONFIGURED"
        return (
            OpenAIAudioTranscriptionProvider(
                api_key=runtime.api_key,
                model=runtime.transcribe_model,
                timeout_seconds=settings.REMOTE_REQUEST_TIMEOUT_SECONDS,
                connect_timeout_seconds=settings.REMOTE_CONNECT_TIMEOUT_SECONDS,
            ),
            None,
        )
    if settings.VOICE_TRANSCRIPTION_PROVIDER == "local":
        if not settings.LOCAL_ASR_MODEL_DIR:
            return None, "LOCAL_ASR_MODEL_REQUIRED"
        try:
            return (
                LocalFasterWhisperProvider(
                    settings.LOCAL_ASR_MODEL_DIR,
                    timeout_seconds=settings.VOICE_ASR_TIMEOUT_SECONDS,
                ),
                None,
            )
        except ValueError:
            return None, "LOCAL_ASR_MODEL_NOT_CACHED"
    return None, "ASR_PROVIDER_UNAVAILABLE"


async def _transcribe(
    db: Session, voice_session: VoiceSession, asset_payload: bytes
) -> tuple[TranscriptResult | None, str | None, ProviderFailure | None]:
    if voice_session.synthetic_fixture:
        if not voice_session.fixture_id:
            return None, "SYNTHETIC_FIXTURE_ID_MISSING", None
        return (
            SyntheticFixtureProvider().transcribe_fixture(voice_session.fixture_id),
            None,
            None,
        )
    provider, reason = _configured_provider(db, voice_session)
    if provider is None:
        return None, reason, None
    remote_provider = provider.provider_name == _AUDIO_PROVIDER
    if remote_provider:
        try:
            _assert_audio_circuit_available(db, voice_session.clinic_id)
        except ProviderCircuitOpen as exc:
            failure = classify_provider_failure(exc)
            return None, failure.code, failure
    with tempfile.TemporaryDirectory(prefix="nightingale-asr-") as temp_name:
        temp_dir = Path(temp_name)
        os.chmod(temp_dir, stat.S_IRWXU)
        audio_path = temp_dir / "normalized.wav"
        write_private_file(audio_path, asset_payload)
        try:
            request_timeout = (
                settings.VOICE_ASR_TIMEOUT_SECONDS
                if provider.provider_name.endswith("-local")
                else settings.REMOTE_REQUEST_TIMEOUT_SECONDS
            )
            result = await asyncio.wait_for(
                provider.transcribe(audio_path),
                timeout=max(1, request_timeout),
            )
        except Exception as exc:
            if remote_provider:
                failure = classify_provider_failure(exc)
                return None, failure.code, failure
            if isinstance(exc, TimeoutError):
                return None, "ASR_TIMEOUT", None
            raise
        if provider.provider_name.endswith("-local") and settings.PYANNOTE_ENABLED:
            ready, readiness_code = pyannote_runtime_status()
            if not ready:
                result = replace(
                    result,
                    warnings=tuple(
                        sorted(
                            {
                                *result.warnings,
                                readiness_code,
                                "LOCAL_DIARIZATION_UNAVAILABLE",
                            }
                        )
                    ),
                )
            else:
                try:
                    assert settings.PYANNOTE_MODEL_DIR is not None
                    diarizer = LocalPyannoteDiarizer(
                        settings.PYANNOTE_MODEL_DIR,
                        timeout_seconds=min(
                            300, max(1, settings.VOICE_ASR_TIMEOUT_SECONDS)
                        ),
                    )
                    turns = await diarizer.diarize(audio_path)
                    result = apply_local_diarization(result, turns)
                except TimeoutError:
                    result = replace(
                        result,
                        warnings=tuple(
                            sorted(
                                {
                                    *result.warnings,
                                    "LOCAL_DIARIZATION_TIMEOUT",
                                    "LOCAL_DIARIZATION_UNAVAILABLE",
                                }
                            )
                        ),
                    )
                except (RuntimeError, ValueError):
                    result = replace(
                        result,
                        warnings=tuple(
                            sorted(
                                {
                                    *result.warnings,
                                    "LOCAL_DIARIZATION_UNAVAILABLE",
                                }
                            )
                        ),
                    )
        return validate_transcript_result(result), None, None


def _normalized_segments(
    result: TranscriptResult,
    asset: AudioAsset,
    *,
    supported_languages: frozenset[LanguageCode] | None = None,
) -> tuple[list[TranscriptSegmentResult], list[str]]:
    # Unit callers without a database exercise the full product language set.
    # Runtime callers always pass the clinic allowlist loaded from PostgreSQL.
    language_policy = (
        CLINIC_CONFIGURABLE_LANGUAGE_CODES
        if supported_languages is None
        else supported_languages
    )
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
        raw_language = (
            segment.source_language
            or segment.detected_language
            or result.detected_language
        )
        provider_source_language = normalize_language_code(raw_language)
        if segment.language_spans:
            try:
                language_spans = validate_addressable_language_spans(
                    segment.text, segment.language_spans
                )
            except ValueError as exc:
                raise VoiceJobError("INVALID_LANGUAGE_SPAN_METADATA") from exc
        else:
            detected_spans = detect_language_spans(
                segment.text,
                source_language=raw_language,
                source_confidence=segment.language_confidence,
            )
            language_spans = tuple(
                AddressableLanguageSpan(
                    start_offset=span.start,
                    end_offset=span.end,
                    language_code=span.source_language,
                    confidence=span.confidence,
                    detection_source=span.detection_source,
                    review_required=span.review_required,
                )
                for span in detected_spans
            )
            validate_addressable_language_spans(segment.text, language_spans)
        policy_violation = any(
            span.language_code == "und" or span.language_code not in language_policy
            for span in language_spans
        )
        language_spans = apply_clinic_language_policy(language_spans, language_policy)
        if policy_violation:
            warnings.append("CLINIC_LANGUAGE_POLICY_REVIEW_REQUIRED")
        qualified_languages = {
            span.language_code for span in language_spans if span.language_code != "und"
        }
        language_review_required = any(span.review_required for span in language_spans)
        if language_review_required:
            source_language = "und"
            language_confidence = None
            warnings.append("MIXED_LANGUAGE_SEGMENT_REVIEW")
        elif len(qualified_languages) > 1:
            # The aggregate segment has no single language, but each clean
            # configured span remains independently eligible for extraction.
            source_language = "und"
            language_confidence = None
            warnings.append("MIXED_LANGUAGE_SEGMENT_REVIEW")
        elif len(qualified_languages) == 1:
            source_language = next(iter(qualified_languages))
            language_confidence = segment.language_confidence
        else:
            source_language = provider_source_language
            language_confidence = segment.language_confidence
            if source_language == "und":
                warnings.append("SOURCE_LANGUAGE_UNAVAILABLE")
        output.append(
            replace(
                segment,
                text_start=start,
                text_end=end,
                detected_language=normalize_language_code(
                    segment.detected_language or result.detected_language
                ),
                source_language=source_language,
                language_confidence=language_confidence,
                language_spans=language_spans,
            )
        )
    if not output:
        raise VoiceJobError("NO_VALID_TRANSCRIPT_SEGMENTS")
    return output, sorted(set(warnings))


def _fact_candidates(
    segments: list[TranscriptSegmentResult],
) -> list[tuple[NormalizedFact, int, int, TranscriptSegmentResult]]:
    output: list[tuple[NormalizedFact, int, int, TranscriptSegmentResult]] = []
    for segment in segments:
        if segment.text_start is None or segment.text_end is None:
            continue
        language_spans = segment.language_spans or (
            AddressableLanguageSpan(
                start_offset=0,
                end_offset=len(segment.text),
                language_code="und",
                confidence=None,
                detection_source="unavailable",
                review_required=True,
            ),
        )
        for language_span in language_spans:
            fragment = segment.text[
                language_span.start_offset : language_span.end_offset
            ]
            for extracted in extract_normalized_facts(
                fragment, source_language=language_span.language_code
            ):
                if extracted.fact_type not in {
                    "allergy",
                    "medication",
                    "dose",
                    "route",
                    "frequency",
                }:
                    continue
                fact = extracted
                if language_span.review_required:
                    fact = replace(
                        fact,
                        review_required=True,
                        source_language=language_span.language_code,
                        polarity=(
                            "unknown" if fact.fact_type == "allergy" else fact.polarity
                        ),
                        value=(
                            "unknown" if fact.fact_type == "allergy" else fact.value
                        ),
                    )
                start = segment.text_start + language_span.start_offset + fact.start
                end = segment.text_start + language_span.start_offset + fact.end
                if not (segment.text_start <= start <= end <= segment.text_end):
                    continue
                output.append((fact, start, end, segment))
    return output


def _apply_calibration(
    segments: list[TranscriptSegmentResult],
    calibration: CalibrationReport | None,
) -> list[TranscriptSegmentResult]:
    """Replace provider self-scores with holdout-calibrated evidence or no score."""

    if calibration is None:
        return [
            replace(item, confidence=None, confidence_source="unavailable")
            for item in segments
        ]
    return [
        replace(
            item,
            confidence=calibration.accuracy_lower_bound,
            confidence_source=f"calibrated:{calibration.id}",
        )
        for item in segments
    ]


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
    supported_languages = clinic_supported_language_codes(db, context.clinic_id)
    segments, span_warnings = _normalized_segments(
        result,
        asset,
        supported_languages=supported_languages,
    )
    calibration = matching_calibration_report(
        db,
        clinic_id=context.clinic_id,
        provider=result.provider,
        exact_model_id=result.model,
        task="voice_transcription",
        request_parameters={
            "response_format": "diarized_json",
            "chunking_strategy": "auto",
        },
        dataset_manifest_sha256=evaluation_manifest_sha256(),
        code_commit=settings.NIGHTINGALE_SOURCE_COMMIT,
    )
    segments = _apply_calibration(segments, calibration)
    agent_state = None
    agent_warnings: list[str] = []
    if settings.VOICE_MULTI_AGENT_PIPELINE:
        agent_state = run_consult_on_segments(
            segments, consult_id=str(voice_session.id)
        )
        fact_candidates, agent_warnings = consult_fact_candidates(agent_state, segments)
    else:
        fact_candidates = _fact_candidates(segments)
    fact_validity = [
        validate_fact_evidence(
            transcript=result.text,
            transcript_start=start,
            transcript_end=end,
            exact_quote=result.text[start:end],
            quote_sha256=hashlib.sha256(result.text[start:end].encode()).hexdigest(),
            segment_start_ms=source_segment.start_ms,
            segment_end_ms=source_segment.end_ms,
            audio_start_ms=source_segment.start_ms,
            audio_end_ms=source_segment.end_ms,
            asset_duration_ms=asset.duration_ms,
        )
        for _fact, start, end, source_segment in fact_candidates
    ]
    fact_valid = bool(fact_candidates) and all(fact_validity)
    preprocessing_warning_map = {
        "silence_review": "SILENCE_REVIEW",
        "clipping_review": "CLIPPING_REVIEW",
        "noise_review": "NOISE_REVIEW",
        "low_signal_review": "LOW_SIGNAL_REVIEW",
        "overlap_review": "MULTI_DEVICE_OVERLAP_REVIEW",
    }
    preprocessing_warnings = {
        warning
        for signal, warning in preprocessing_warning_map.items()
        if asset.preprocessing_json.get(signal) is True
    }
    warnings = sorted(
        {
            *result.warnings,
            *span_warnings,
            *preprocessing_warnings,
            *agent_warnings,
            *(consult_warning_codes(agent_state) if agent_state is not None else ()),
        }
    )
    low_confidence = calibration is not None and calibration.confidence_band == "low"
    confidence_unavailable = calibration is None
    overlap = any(item.overlap_group_id for item in segments)
    if low_confidence:
        warnings.append("LOW_CONFIDENCE_REVIEW")
    if confidence_unavailable:
        warnings.append("CONFIDENCE_UNAVAILABLE")
    if overlap:
        warnings.append("OVERLAP_REVIEW")
    if not fact_candidates:
        warnings.append("NO_STRUCTURED_FACTS")
    elif not fact_valid:
        warnings.append("INVALID_FACT_EVIDENCE")
    if any(candidate[0].review_required for candidate in fact_candidates):
        warnings.append("CLINICAL_LANGUAGE_OR_CONCEPT_REVIEW_REQUIRED")
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
    if agent_state is not None:
        summary = consult_summary(agent_state)
    else:
        summary = (
            "Recorded clinical information is ready for clinician review."
            if fact_candidates
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
        detected_language=normalize_language_code(result.detected_language),
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
        consult_agent_json=(
            consult_agent_payload(agent_state) if agent_state is not None else {}
        ),
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
            speaker_ids_json=list(item.speaker_ids),
            detected_language=item.detected_language,
            source_language=item.source_language or "und",
            language_confidence=item.language_confidence,
            language_spans_json=[
                language_span_payload(span) for span in item.language_spans
            ],
            confidence=item.confidence,
            confidence_source=item.confidence_source,
            overlap_group_id=item.overlap_group_id,
            provider=result.provider,
            model=result.model,
        )
        db.add(row)
        persisted_segments.append((row, item))
    db.flush()
    for ordinal, candidate in enumerate(fact_candidates):
        fact, start, end, source_segment = candidate
        quote = result.text[start:end]
        value = (
            f"{fact.key} allergy:{fact.polarity}"
            if fact.fact_type == "allergy"
            else fact.value
        )
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
        if valid:
            db.add(
                ClinicalFact(
                    id=fact_id,
                    clinic_id=context.clinic_id,
                    session_id=voice_session.id,
                    revision_id=revision.id,
                    segment_id=segment_row.id,
                    ordinal=ordinal,
                    fact_type=fact.fact_type,
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
    outcome_error_code: str | None = None,
    outcome_error_class: str | None = None,
    provider_failure: ProviderFailure | None = None,
) -> Job:
    terminal_target_state = "needs_review" if needs_review else "ready"
    if voice_session.state != terminal_target_state and voice_session.state not in {
        "transcribing",
        "redacting",
        "extracting",
    }:
        raise VoiceJobError("VOICE_STATE_TRANSITION_CONFLICT")
    completed_at = get_datetime_utc()
    next_retry_at: datetime | None = None
    circuit: ProviderCircuitState | None = None
    if provider_failure is not None:
        next_retry_at, circuit = _record_audio_provider_failure(
            db,
            job,
            provider_failure,
            attempt_no=attempt.attempt_no,
        )
        outcome_error_code = provider_failure.code
        outcome_error_class = provider_failure.failure_class
    # A retryable provider failure has not completed clinical processing. Keep
    # the session in its durable processing stage so both the session and job
    # polling loops continue until the worker retries or exhausts attempts.
    target_state = (
        voice_session.state if next_retry_at is not None else terminal_target_state
    )
    attempt.status = "failed" if provider_failure is not None else "completed"
    attempt.error_code = outcome_error_code
    attempt.error_class = outcome_error_class
    attempt.completed_at = completed_at
    job.state = (
        "failed"
        if next_retry_at is not None
        else "needs_review"
        if needs_review
        else "completed"
    )
    job.locked_by = None
    job.locked_until = None
    job.error_code = outcome_error_code
    job.error_class = outcome_error_class
    job.next_run_at = next_retry_at
    job.provider_outage = bool(provider_failure and provider_failure.retryable)
    job.last_attempt_at = completed_at
    if outcome_error_code is None:
        job.delayed_at = None
    else:
        job.delayed_at = completed_at
        if outcome_error_class == "timeout":
            job.timed_out_at = completed_at
        history = list(job.retry_history_json)
        history.append(
            {
                "attempt": attempt.attempt_no,
                "error_code": outcome_error_code,
                "error_class": outcome_error_class or "unavailable",
                "attempted_at": completed_at.isoformat(),
                "next_retry_at": (
                    next_retry_at.isoformat() if next_retry_at is not None else None
                ),
                "provider": _AUDIO_PROVIDER if provider_failure else None,
                "capability": _AUDIO_CAPABILITY if provider_failure else None,
                "circuit_state": circuit.state if circuit is not None else None,
            }
        )
        job.retry_history_json = history
    job.updated_at = completed_at
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
        action=(
            "voice.processing_delayed"
            if next_retry_at is not None
            else "voice.processing_completed"
        ),
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
            if voice_session.state not in _VOICE_PROCESSING_STATES:
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
                # FFmpeg is synchronous, bounded external work. Re-fence the
                # uncommitted immutable asset after it returns so revocation,
                # lease expiry, or a reclaim during preprocessing rolls the
                # asset back instead of authoring it under a stale claim.
                job, attempt = _claim_is_current(db, context, job_id, token)
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
            result, unavailable_reason, provider_failure = await _transcribe(
                db, voice_session, asset_payload
            )
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
                    outcome_error_code=voice_session.error_code,
                    outcome_error_class=(
                        provider_failure.failure_class
                        if provider_failure is not None
                        else "timeout"
                        if voice_session.error_code == "ASR_TIMEOUT"
                        else "unavailable"
                    ),
                    provider_failure=provider_failure,
                )
            if result.provider == _AUDIO_PROVIDER:
                _record_audio_provider_success(db, job)
            if voice_session.state == "transcribing":
                voice_session = _transition_state(
                    db,
                    context,
                    voice_session.id,
                    expected={"transcribing"},
                    # Voice audio remains local by default and structured
                    # extraction operates on the local transcript. Do not
                    # claim a no-op redaction boundary that has transformed
                    # neither audio nor transcript text.
                    target="extracting",
                )
                db.commit()
                job, attempt = _claim_is_current(db, context, job_id, token)
                voice_session = _session_from_payload(db, context, job, payload)
            # Backward-compatible recovery for jobs persisted before the
            # no-op redacting stage was removed.
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
                    speaker_ids=tuple(row.speaker_ids_json),
                    detected_language=row.detected_language,
                    source_language=row.source_language,
                    language_confidence=row.language_confidence,
                    language_spans=tuple(
                        language_span_from_payload(payload)
                        for payload in row.language_spans_json
                    ),
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
        # Provisional captions are never the clinical record. Once the
        # immutable final revision exists, advertise that it has replaced the
        # transient live view regardless of whether that view was available.
        voice_session.live_transcript_status = "replaced"
        voice_session.live_transcript_error_code = None
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
        current_voice_session: VoiceSession | None = None
        try:
            # Do not decrypt the failed payload again: malformed ciphertext or
            # JSON may be the original exception. The trusted FK written at
            # enqueue time is sufficient to locate the active session, and the
            # savepoint ensures even a locator failure cannot block job failure.
            with db.begin_nested():
                current_voice_session = db.exec(
                    select(VoiceSession)
                    .where(
                        VoiceSession.clinic_id == context.clinic_id,
                        VoiceSession.processing_job_id == job.id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).first()
        except Exception:
            # A changed/broken binding is itself a fence: never mutate a
            # different session, but still persist the terminal job/attempt.
            current_voice_session = None
        if (
            current_voice_session is not None
            and current_voice_session.published_entry_id is None
            and (
                current_voice_session.state in _VOICE_PROCESSING_STATES
                or (
                    current_voice_session.state == "needs_review"
                    and job.attempt_count >= job.max_attempts
                )
            )
        ):
            # Generic failures do not schedule an automatic retry; claim_job
            # requires an explicit clinical retry. Initial processing has no
            # derived revision to protect, so release it to manual review at
            # once. A failed reanalysis keeps its CAS barrier until that
            # explicit retry (or exhaustion), preventing publication of the
            # pre-reanalysis revision as if the requested work had succeeded.
            has_existing_revision = (
                current_voice_session.current_transcript_revision_id is not None
            )
            attempts_exhausted = job.attempt_count >= job.max_attempts
            if not has_existing_revision or attempts_exhausted:
                warnings = {
                    *current_voice_session.warning_codes_json,
                    "PROCESSING_FAILED",
                }
                if attempts_exhausted:
                    warnings.add("VOICE_WORKER_ATTEMPTS_EXHAUSTED")
                current_voice_session.state = "needs_review"
                current_voice_session.error_code = code
                current_voice_session.warning_codes_json = sorted(warnings)
                current_voice_session.updated_at = get_datetime_utc()
                db.add(current_voice_session)
        db.commit()
        return job
