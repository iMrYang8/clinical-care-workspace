from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime

from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    LiveTranscriptAvailability,
    LiveTranscriptStatus,
    ProvisionalSafetyAlert,
    VoiceSession,
    get_datetime_utc,
)
from app.services.conflicts import (
    NormalizedFact,
    extract_normalized_facts,
    normalize_language_code,
)
from app.services.nightingale import emit_change
from app.services.voice.egress_policy import remote_audio_egress_denial
from app.services.voice.language import clinic_supported_language_codes
from app.services.voice.live_providers import (
    DeterministicLiveTranscriptionProvider,
    LiveTranscriptionProvider,
    OpenAILiveTranscriptionProvider,
)

_CAPTURE_STATES = {"recording"}
_LIVE_CONSULT_MAX_TURNS = 50
_live_consult_buffers: dict[uuid.UUID, list] = {}


def clear_live_consult_buffer(session_id: uuid.UUID | None = None) -> None:
    """Drop in-memory completed-caption state. Tests call this between cases."""

    if session_id is None:
        _live_consult_buffers.clear()
        return
    _live_consult_buffers.pop(session_id, None)


def configured_live_provider(
    voice_session: VoiceSession,
    *,
    db: Session | None = None,
) -> tuple[LiveTranscriptionProvider | None, str | None, str | None, str | None]:
    provider: LiveTranscriptionProvider
    if voice_session.live_transcript_status == "replaced":
        return None, "FINAL_TRANSCRIPT_AVAILABLE", None, None
    if not settings.LIVE_TRANSCRIPT_ENABLED:
        return None, "LIVE_TRANSCRIPT_NOT_CONFIGURED", None, None
    provider_kind = settings.LIVE_TRANSCRIPT_PROVIDER
    if provider_kind == "disabled":
        return None, "LIVE_TRANSCRIPT_PROVIDER_DISABLED", None, None
    if voice_session.state not in _CAPTURE_STATES:
        return None, "VOICE_SESSION_NOT_RECORDING", None, None
    if provider_kind == "deterministic":
        if (
            settings.FASTAPI_ENV != "development"
            or not settings.ENABLE_DEMO_AUTH
            or not voice_session.synthetic_fixture
            or not voice_session.fixture_id
        ):
            return None, "LIVE_TRANSCRIPT_FIXTURE_REQUIRED", None, None
        try:
            provider = DeterministicLiveTranscriptionProvider(
                fixture_id=voice_session.fixture_id,
                max_frame_bytes=settings.LIVE_TRANSCRIPT_MAX_FRAME_BYTES,
            )
        except ValueError:
            return None, "LIVE_TRANSCRIPT_FIXTURE_UNKNOWN", None, None
        return provider, None, provider.provider_name, provider.model
    if provider_kind == "openai":
        if settings.STRICT_NO_AUDIO_EGRESS:
            return None, "STRICT_NO_AUDIO_EGRESS", None, None
        if not settings.REMOTE_AUDIO_EGRESS_ENABLED:
            return None, "REMOTE_AUDIO_EGRESS_DISABLED", None, None
        policy_denial = remote_audio_egress_denial(db, voice_session)
        if policy_denial is not None:
            return None, policy_denial, None, None
        if not settings.OPENAI_API_KEY or not settings.OPENAI_LIVE_TRANSCRIBE_MODEL:
            return None, "OPENAI_LIVE_TRANSCRIPT_NOT_CONFIGURED", None, None
        if not settings.OPENAI_LIVE_TRANSCRIBE_MODEL.startswith("gpt-live-transcribe"):
            return None, "OPENAI_LIVE_TRANSCRIPT_MODEL_UNSUPPORTED", None, None
        provider = OpenAILiveTranscriptionProvider(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_LIVE_TRANSCRIBE_MODEL,
            max_frame_bytes=settings.LIVE_TRANSCRIPT_MAX_FRAME_BYTES,
            timeout_seconds=settings.LIVE_TRANSCRIPT_PROVIDER_TIMEOUT_SECONDS,
        )
        return provider, None, provider.provider_name, provider.model
    return None, "LIVE_TRANSCRIPT_PROVIDER_UNAVAILABLE", None, None


def live_availability(
    voice_session: VoiceSession, *, db: Session | None = None
) -> LiveTranscriptAvailability:
    provider, reason, provider_name, model = configured_live_provider(
        voice_session, db=db
    )
    if voice_session.live_transcript_status == "replaced":
        return LiveTranscriptAvailability(
            available=False,
            status="replaced",
            reason_code="FINAL_TRANSCRIPT_AVAILABLE",
        )
    if provider is None:
        return LiveTranscriptAvailability(
            available=False, status="unavailable", reason_code=reason
        )
    if voice_session.live_transcript_status == "needs_review":
        return LiveTranscriptAvailability(
            available=True,
            status="needs_review",
            reason_code=voice_session.live_transcript_error_code,
            provider=provider_name,
            model=model,
        )
    return LiveTranscriptAvailability(
        available=True,
        status="available",
        provider=provider_name,
        model=model,
    )


def set_live_transcript_status(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    *,
    status: LiveTranscriptStatus,
    reason_code: str | None,
) -> LiveTranscriptStatus:
    if voice_session.clinic_id != context.clinic_id:
        raise ValueError("Voice session tenant mismatch")
    if voice_session.live_transcript_status == "replaced" and status != "replaced":
        # A durable immutable transcript is authoritative. Late socket errors,
        # disconnect cleanup, and reconnect attempts may not downgrade it.
        return "replaced"
    if (
        voice_session.live_transcript_status == status
        and voice_session.live_transcript_error_code == reason_code
    ):
        return status
    voice_session.live_transcript_status = status
    voice_session.live_transcript_error_code = reason_code
    voice_session.updated_at = get_datetime_utc()
    db.add(voice_session)
    emit_change(
        db,
        context,
        action="voice.live_transcript_status",
        resource_type="voice_session",
        resource_id=voice_session.id,
        metadata={"status": status, "reason_code": reason_code},
    )
    return status


def safety_identifier(context: RequestContext) -> str:
    # The provider receives a stable, privacy-preserving identifier rather than
    # a user, patient, membership, or clinic UUID.
    value = f"{context.clinic_id}:{context.user_id}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), value, hashlib.sha256).hexdigest()


def _agent_allergy_facts_for_completed_caption(
    voice_session: VoiceSession,
    *,
    text: str,
    source_language: str | None,
) -> list[NormalizedFact]:
    """Re-run consult agents on a bounded completed-caption buffer.

    Proposals only. Does not write ConflictCase rows. Flag-off callers never
    reach this function.
    """

    from app.services.voice.multi_agent import (
        consult_fact_candidates,
        run_consult_on_segments,
    )
    from app.services.voice.providers.base import TranscriptSegmentResult

    buffer: list[TranscriptSegmentResult] = _live_consult_buffers.setdefault(
        voice_session.id, []
    )
    index = len(buffer)
    speaker = f"SPEAKER_{index:02d}"
    segment = TranscriptSegmentResult(
        text=text,
        start_ms=index * 1_000,
        end_ms=index * 1_000 + 800,
        speaker_id=speaker,
        detected_language=source_language,
        confidence=0.9,
        confidence_source="live_caption",
        overlap_group_id=None,
        text_start=0,
        text_end=len(text),
        source_language=source_language,
        language_confidence=0.9,
        speaker_ids=(speaker,),
    )
    buffer.append(segment)
    del buffer[:-_LIVE_CONSULT_MAX_TURNS]
    state = run_consult_on_segments(buffer, consult_id=f"live-{voice_session.id.hex}")
    candidates, _extra = consult_fact_candidates(state, buffer)
    latest = buffer[-1]
    facts: list[NormalizedFact] = []
    for fact, _start, _end, origin in candidates:
        if fact.fact_type != "allergy":
            continue
        if origin is not latest:
            continue
        facts.append(fact)
    return facts


def persist_completed_safety_alerts(
    db: Session,
    context: RequestContext,
    voice_session: VoiceSession,
    *,
    source_event_id: str | None,
    text: str,
    source_language: str | None,
    completed_segment_at: datetime | None = None,
) -> list[ProvisionalSafetyAlert]:
    """Persist deduplicated provisional alerts from a completed live segment."""

    if voice_session.clinic_id != context.clinic_id:
        raise ValueError("Voice session tenant mismatch")
    normalized_text = text
    if not normalized_text.strip():
        return []
    detected_at = get_datetime_utc()
    completed_at = completed_segment_at or detected_at
    language = normalize_language_code(source_language)
    aggregate_language_hint = (
        source_language is None
        or source_language.strip().casefold()
        in {
            "auto",
            "multilingual",
            "mixed",
        }
    )
    language_hint = None if aggregate_language_hint else source_language
    supported_languages = clinic_supported_language_codes(db, context.clinic_id)
    explicit_language_disallowed = not aggregate_language_hint and (
        language == "und" or language not in supported_languages
    )
    facts = list(
        extract_normalized_facts(
            normalized_text,
            source_language=language_hint,
        )
    )
    if settings.VOICE_MULTI_AGENT_PIPELINE:
        facts.extend(
            _agent_allergy_facts_for_completed_caption(
                voice_session,
                text=normalized_text,
                source_language=language_hint,
            )
        )
    created: list[ProvisionalSafetyAlert] = []
    for fact in facts:
        if fact.fact_type != "allergy":
            continue
        fact_language = normalize_language_code(fact.source_language)
        policy_review_required = (
            fact.review_required
            or explicit_language_disallowed
            or fact_language == "und"
            or fact_language not in supported_languages
        )
        concept_code = (
            f"allergy:{fact.key}"
            if not policy_review_required
            else "allergy:review_required"
        )
        polarity = "unknown" if policy_review_required else fact.polarity
        deduplication_key = hashlib.sha256(
            (
                f"{voice_session.id}:{concept_code}:{fact.assertion_scope}:"
                f"{polarity}:{fact.key if policy_review_required else ''}"
            ).encode()
        ).hexdigest()
        existing = db.exec(
            select(ProvisionalSafetyAlert).where(
                ProvisionalSafetyAlert.clinic_id == context.clinic_id,
                ProvisionalSafetyAlert.session_id == voice_session.id,
                ProvisionalSafetyAlert.deduplication_key == deduplication_key,
            )
        ).first()
        if existing is not None:
            continue
        alert_id = uuid.uuid4()
        quote = normalized_text[fact.start : fact.end]
        alert = ProvisionalSafetyAlert(
            id=alert_id,
            clinic_id=context.clinic_id,
            patient_id=voice_session.patient_id,
            session_id=voice_session.id,
            source_event_id=(
                source_event_id[:160]
                if source_event_id
                else f"completed:{hashlib.sha256(normalized_text.encode()).hexdigest()[:24]}"
            ),
            source_text_ciphertext=field_codec.encrypt_text(
                context.clinic_id,
                "provisional_safety_alert.source_text",
                alert_id,
                quote,
            ),
            source_text_sha256=hashlib.sha256(quote.encode()).hexdigest(),
            source_start_offset=fact.start,
            source_end_offset=fact.end,
            source_language=(fact_language if fact_language != "und" else language),
            concept_code=concept_code,
            assertion_scope=fact.assertion_scope,
            polarity=polarity,
            deduplication_key=deduplication_key,
            severity="critical" if not policy_review_required else "high",
            state="pending",
            completed_segment_at=completed_at,
            detected_at=detected_at,
        )
        db.add(alert)
        created.append(alert)
    if created:
        db.flush()
        for alert in created:
            emit_change(
                db,
                context,
                action="voice.provisional_safety_alert_detected",
                resource_type="provisional_safety_alert",
                resource_id=alert.id,
                metadata={
                    "concept_code": alert.concept_code,
                    "severity": alert.severity,
                },
            )
    return created
