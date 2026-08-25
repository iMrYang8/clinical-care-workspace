import uuid

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    ClinicalGlanceCard,
    ClinicalGlancePublic,
    GlanceCard,
    GlancePublic,
    PatientGlanceSnapshot,
    PatientsPublic,
    PatientTimeline,
)
from app.services.nightingale import get_patient, list_patients, read_glance, timeline

router = APIRouter(prefix="/patients", tags=["patients"])


def _require_patient_data_role(context: CurrentContext) -> None:
    if context.role in {"admin", "worker"}:
        raise HTTPException(status_code=403, detail="Role cannot access clinical data")


@router.get("", response_model=PatientsPublic)
@router.get("/", response_model=PatientsPublic, include_in_schema=False)
def patients(session: SessionDep, context: CurrentContext) -> PatientsPublic:
    _require_patient_data_role(context)
    data = list_patients(session, context)
    return PatientsPublic(data=data, count=len(data))


@router.get("/{patient_id}/timeline", response_model=PatientTimeline)
def patient_timeline(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> PatientTimeline:
    _require_patient_data_role(context)
    data = timeline(session, context, patient_id)
    return PatientTimeline(data=data, count=len(data))


@router.get(
    "/{patient_id}/glance",
    response_model=GlancePublic | ClinicalGlancePublic,
    response_model_exclude_none=True,
)
def patient_glance(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> GlancePublic | ClinicalGlancePublic:
    _require_patient_data_role(context)
    get_patient(session, context, patient_id)
    snapshot = session.exec(
        select(PatientGlanceSnapshot).where(
            PatientGlanceSnapshot.clinic_id == context.clinic_id,
            PatientGlanceSnapshot.patient_id == patient_id,
        )
    ).first()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Glance snapshot not ready")
    cards, generated_at = read_glance(snapshot)
    if context.role == "patient":
        # Defence in depth: old/internal snapshot cards never cross the patient DTO.
        cards = [card for card in cards if card.get("patient_facing") is True]
        patient_cards = []
        for card in cards[:5]:
            safe = {
                key: value for key, value in card.items() if key != "score_components"
            }
            patient_cards.append(GlanceCard.model_validate(safe))
        return GlancePublic(
            patient_id=patient_id,
            generated_at=generated_at,
            cards=patient_cards,
        )
    return ClinicalGlancePublic(
        patient_id=patient_id,
        generated_at=generated_at,
        cards=[
            ClinicalGlanceCard.model_validate(
                card | {"score_components": card.get("score_components", {})}
            )
            for card in cards[:5]
        ],
    )
