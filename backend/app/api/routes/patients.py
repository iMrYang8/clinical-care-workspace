import uuid

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    GlanceCard,
    GlancePublic,
    PatientGlanceSnapshot,
    PatientsPublic,
    PatientTimeline,
)
from app.services.nightingale import get_patient, list_patients, read_glance, timeline

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("", response_model=PatientsPublic)
@router.get("/", response_model=PatientsPublic, include_in_schema=False)
def patients(session: SessionDep, context: CurrentContext) -> PatientsPublic:
    data = list_patients(session, context)
    return PatientsPublic(data=data, count=len(data))


@router.get("/{patient_id}/timeline", response_model=PatientTimeline)
def patient_timeline(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> PatientTimeline:
    data = timeline(session, context, patient_id)
    return PatientTimeline(data=data, count=len(data))


@router.get("/{patient_id}/glance", response_model=GlancePublic)
def patient_glance(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> GlancePublic:
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
    return GlancePublic(
        patient_id=patient_id,
        generated_at=generated_at,
        cards=[GlanceCard.model_validate(card) for card in cards[:5]],
    )
