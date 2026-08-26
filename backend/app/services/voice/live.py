from __future__ import annotations

import hashlib
import hmac

from sqlmodel import Session

from app.api.deps import RequestContext
from app.core.config import settings
from app.models import (
    LiveTranscriptAvailability,
    LiveTranscriptStatus,
    VoiceSession,
    get_datetime_utc,
)
from app.services.nightingale import emit_change
from app.services.voice.live_providers import (
    DeterministicLiveTranscriptionProvider,
    LiveTranscriptionProvider,
    OpenAILiveTranscriptionProvider,
)

_CAPTURE_STATES = {"recording"}


def configured_live_provider(
    voice_session: VoiceSession,
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


def live_availability(voice_session: VoiceSession) -> LiveTranscriptAvailability:
    provider, reason, provider_name, model = configured_live_provider(voice_session)
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
