import uuid

from fastapi import APIRouter, HTTPException
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    ClinicalGlanceCard,
    ClinicalGlancePublic,
    Entry,
    EntryVersion,
    GlancePublic,
    Highlight,
    PatientGlanceCard,
    PatientGlanceSnapshot,
    PatientsPublic,
    PatientTimeline,
    ProvenancePointer,
)
from app.services.nightingale import get_patient, list_patients, read_glance, timeline

router = APIRouter(prefix="/patients", tags=["patients"])


def _require_patient_data_role(context: CurrentContext) -> None:
    if context.role in {"admin", "worker"}:
        raise HTTPException(status_code=403, detail="Role cannot access clinical data")


def _patient_card_source_is_currently_visible(
    session: SessionDep,
    context: CurrentContext,
    patient_id: uuid.UUID,
    card: dict[str, object],
) -> bool:
    """Revalidate the at-most-five cached cards against current sharing state."""

    try:
        highlight_id = uuid.UUID(str(card["highlight_id"]))
        pointer_id = uuid.UUID(str(card["provenance_pointer_id"]))
    except (KeyError, ValueError):
        return False
    highlight = session.exec(
        select(Highlight).where(
            Highlight.id == highlight_id,
            Highlight.clinic_id == context.clinic_id,
            Highlight.patient_id == patient_id,
        )
    ).first()
    if (
        highlight is None
        or not highlight.patient_facing
        or (highlight.status != "accepted" and not highlight.pinned)
        or highlight.anchor_state != "resolved"
        or highlight.review_required
    ):
        return False
    entry = session.exec(
        select(Entry).where(
            Entry.id == highlight.entry_id,
            Entry.clinic_id == context.clinic_id,
            col(Entry.patient_facing).is_(True),
        )
    ).first()
    version = session.exec(
        select(EntryVersion).where(
            EntryVersion.id == highlight.source_entry_version_id,
            EntryVersion.entry_id == highlight.entry_id,
            EntryVersion.clinic_id == context.clinic_id,
            col(EntryVersion.patient_facing).is_(True),
        )
    ).first()
    pointer = session.exec(
        select(ProvenancePointer).where(
            ProvenancePointer.id == pointer_id,
            ProvenancePointer.highlight_id == highlight.id,
            ProvenancePointer.entry_version_id == highlight.source_entry_version_id,
            ProvenancePointer.clinic_id == context.clinic_id,
        )
    ).first()
    return (
        entry is not None
        and entry.patient_id == patient_id
        and version is not None
        and pointer is not None
        and pointer.anchor_state == "resolved"
        and not pointer.review_required
    )


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
    cards, generated_at = read_glance(
        snapshot, patient_facing=context.role == "patient"
    )
    if context.role == "patient":
        # Defence in depth: old/internal snapshot cards never cross the patient DTO.
        cards = [
            card
            for card in cards
            if card.get("patient_facing") is True
            and _patient_card_source_is_currently_visible(
                session, context, patient_id, card
            )
        ]
        patient_cards: list[PatientGlanceCard] = []
        for card in cards[:5]:
            patient_cards.append(
                PatientGlanceCard(
                    highlight_id=uuid.UUID(str(card["highlight_id"])),
                    label=str(card["label"]),
                    provenance_pointer_id=uuid.UUID(str(card["provenance_pointer_id"])),
                )
            )
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
