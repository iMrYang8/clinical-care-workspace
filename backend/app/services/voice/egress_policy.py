from __future__ import annotations

from sqlmodel import Session, select

from app.models import ClinicOperationalSetting, VoiceSession


def remote_audio_egress_denial(
    db: Session | None, voice_session: VoiceSession
) -> str | None:
    """Return a stable denial code unless clinic policy and session consent agree."""

    if db is None:
        return "CLINIC_AUDIO_EGRESS_POLICY_REQUIRED"
    operational = db.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == voice_session.clinic_id
        )
    ).first()
    if operational is None or not operational.remote_audio_egress_enabled:
        return "CLINIC_REMOTE_AUDIO_EGRESS_DISABLED"
    consented = voice_session.remote_audio_consent_at is not None
    if not consented:
        return "REMOTE_AUDIO_EGRESS_CONSENT_REQUIRED"
    return None
