from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlmodel import Session, select

from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import ClinicAISetting


@dataclass(frozen=True)
class ClinicAIRuntime:
    api_key: str | None
    fast_model: str
    careful_model: str | None
    transcribe_model: str
    source: str


def clinic_ai_runtime(session: Session, clinic_id: uuid.UUID) -> ClinicAIRuntime:
    row = session.exec(
        select(ClinicAISetting).where(ClinicAISetting.clinic_id == clinic_id)
    ).first()
    if row is None:
        return ClinicAIRuntime(
            api_key=settings.OPENAI_API_KEY,
            fast_model=settings.OPENAI_EXTRACT_MODEL or "",
            careful_model=settings.OPENAI_REVIEW_MODEL,
            transcribe_model=settings.OPENAI_TRANSCRIBE_MODEL or "",
            source="environment",
        )
    clinic_api_key = (
        field_codec.decrypt_text(
            clinic_id,
            "clinic_ai_setting.api_key",
            row.id,
            row.api_key_ciphertext,
        )
        if row.api_key_ciphertext
        else None
    )
    api_key = clinic_api_key or settings.OPENAI_API_KEY
    return ClinicAIRuntime(
        api_key=api_key,
        fast_model=row.fast_model,
        careful_model=row.careful_model,
        transcribe_model=row.transcribe_model,
        source="clinic" if clinic_api_key else "environment",
    )
