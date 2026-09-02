import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.core.config import settings
from app.models import (
    ClinicalGlanceCard,
    ClinicalGlancePublic,
    ClinicMembership,
    Entry,
    EntryVersion,
    GlancePublic,
    Highlight,
    PatientGlanceCard,
    PatientGlanceSnapshot,
    PatientPublication,
    PatientPublicationAcknowledgement,
    PatientPublicationReceiptPublic,
    PatientsPublic,
    PatientsSearchRequest,
    PatientTimeline,
    ProvenancePointer,
    ProviderCircuitState,
    PublicationCorrectionOutreach,
    User,
)
from app.services.nightingale import (
    decrypt_version,
    get_patient,
    list_patients,
    read_glance,
    read_review_glance,
    requalify_glance_on_read,
    timeline,
)

router = APIRouter(prefix="/patients", tags=["patients"])


def _require_patient_data_role(context: CurrentContext) -> None:
    if context.role == "worker":
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
    publication = session.exec(
        select(PatientPublication).where(
            PatientPublication.clinic_id == context.clinic_id,
            PatientPublication.patient_id == patient_id,
            PatientPublication.entry_id == highlight.entry_id,
            PatientPublication.entry_version_id == highlight.source_entry_version_id,
            col(PatientPublication.withdrawn_at).is_(None),
        )
    ).first()
    return (
        entry is not None
        and entry.patient_id == patient_id
        and version is not None
        and pointer is not None
        and publication is not None
        and pointer.anchor_state == "resolved"
        and not pointer.review_required
    )


@router.get("", response_model=PatientsPublic)
@router.get("/", response_model=PatientsPublic, include_in_schema=False)
def patients(
    session: SessionDep,
    context: CurrentContext,
    request: Request,
    visit_scope: Literal["all", "today", "previous"] = Query(default="all"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> PatientsPublic:
    _require_patient_data_role(context)
    if "search" in request.query_params:
        raise HTTPException(
            status_code=422,
            detail={"code": "SEARCH_BODY_REQUIRED", "method": "POST"},
        )
    data, count = list_patients(
        session,
        context,
        search=None,
        visit_scope=visit_scope,
        offset=offset,
        limit=limit,
    )
    return PatientsPublic(data=data, count=count, offset=offset, limit=limit)


@router.post("/search", response_model=PatientsPublic)
def search_patients(
    body: PatientsSearchRequest,
    session: SessionDep,
    context: CurrentContext,
) -> PatientsPublic:
    """Search identifiers in a request body so reverse proxies never log them."""

    _require_patient_data_role(context)
    data, count = list_patients(
        session,
        context,
        search=body.search,
        visit_scope=body.visit_scope,
        offset=body.offset,
        limit=body.limit,
    )
    return PatientsPublic(
        data=data,
        count=count,
        offset=body.offset,
        limit=body.limit,
    )


@router.get("/{patient_id}/timeline", response_model=PatientTimeline)
def patient_timeline(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> PatientTimeline:
    _require_patient_data_role(context)
    data = timeline(session, context, patient_id)
    return PatientTimeline(data=data, count=len(data))


@router.get(
    "/{patient_id}/publication-receipts",
    response_model=list[PatientPublicationReceiptPublic],
)
def patient_publication_receipts(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> list[PatientPublicationReceiptPublic]:
    if context.role != "patient":
        raise HTTPException(status_code=403, detail="Patient portal role required")
    get_patient(session, context, patient_id)
    publications = session.exec(
        select(PatientPublication)
        .where(
            PatientPublication.clinic_id == context.clinic_id,
            PatientPublication.patient_id == patient_id,
        )
        .order_by(col(PatientPublication.approved_at).desc())
    ).all()
    receipts: list[PatientPublicationReceiptPublic] = []
    for publication in publications:
        version = session.get(EntryVersion, publication.entry_version_id)
        membership = session.get(
            ClinicMembership, publication.approved_by_membership_id
        )
        approver = session.get(User, membership.user_id) if membership else None
        if version is None or version.clinic_id != context.clinic_id:
            continue
        title, _ = decrypt_version(version)
        replacement = session.exec(
            select(PatientPublication).where(
                PatientPublication.clinic_id == context.clinic_id,
                PatientPublication.patient_id == patient_id,
                PatientPublication.supersedes_publication_id == publication.id,
            )
        ).first()
        related_publication_ids = {publication.id}
        if publication.supersedes_publication_id is not None:
            related_publication_ids.add(publication.supersedes_publication_id)
        if replacement is not None:
            related_publication_ids.add(replacement.id)
        acknowledgement = session.exec(
            select(PatientPublicationAcknowledgement)
            .where(
                PatientPublicationAcknowledgement.clinic_id == context.clinic_id,
                PatientPublicationAcknowledgement.patient_id == patient_id,
                col(PatientPublicationAcknowledgement.publication_id).in_(
                    related_publication_ids
                ),
                PatientPublicationAcknowledgement.event_type == "acknowledged",
            )
            .order_by(col(PatientPublicationAcknowledgement.acknowledged_at).desc())
        ).first()
        outreach = session.exec(
            select(PublicationCorrectionOutreach).where(
                PublicationCorrectionOutreach.clinic_id == context.clinic_id,
                PublicationCorrectionOutreach.patient_id == patient_id,
                (
                    PublicationCorrectionOutreach.withdrawn_publication_id
                    == publication.id
                )
                | (
                    PublicationCorrectionOutreach.replacement_publication_id
                    == publication.id
                ),
            )
        ).first()
        acknowledgement_state: Literal["not_required", "pending", "acknowledged"] = (
            "not_required"
        )
        if acknowledgement is not None:
            acknowledgement_state = "acknowledged"
        elif outreach is not None:
            acknowledgement_state = "pending"
        elif replacement is not None or publication.supersedes_publication_id:
            acknowledgement_state = "pending"
        replacement_title: str | None = None
        if replacement is not None:
            replacement_version = session.get(
                EntryVersion, replacement.entry_version_id
            )
            if (
                replacement_version is not None
                and replacement_version.clinic_id == context.clinic_id
            ):
                replacement_title, _ = decrypt_version(replacement_version)
        receipts.append(
            PatientPublicationReceiptPublic(
                publication_id=publication.id,
                entry_title=title,
                approved_by_name=(
                    approver.full_name or str(approver.email)
                    if approver
                    else "Clinician"
                ),
                approved_at=publication.approved_at,
                withdrawn_at=publication.withdrawn_at,
                status="withdrawn" if publication.withdrawn_at else "active",
                replacement_publication_id=(
                    replacement.id if replacement is not None else None
                ),
                acknowledged_at=(
                    acknowledgement.acknowledged_at
                    if acknowledgement is not None
                    else None
                ),
                outreach_status=outreach.status if outreach is not None else None,
                acknowledgement_state=acknowledgement_state,
                outreach_required=bool(
                    outreach is not None and outreach.status == "pending"
                ),
                replacement_entry_title=replacement_title,
            )
        )
    return receipts


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
    snapshot = requalify_glance_on_read(session, context, patient_id)
    # Requalification also records the complete candidate/exposure set.  A
    # read must persist that telemetry (and any confidence demotion) rather
    # than letting Session teardown roll it back.
    session.commit()
    session.refresh(snapshot)
    cards, generated_at = read_glance(
        snapshot, patient_facing=context.role == "patient"
    )
    importance_mode = cast(
        Literal["disabled", "shadow", "active"],
        (
            snapshot.importance_mode
            if snapshot.importance_mode in {"disabled", "shadow", "active"}
            else "shadow"
        ),
    )
    age_seconds = max(0, int((datetime.now(UTC) - generated_at).total_seconds()))
    circuit = session.exec(
        select(ProviderCircuitState).where(
            ProviderCircuitState.clinic_id == context.clinic_id,
            ProviderCircuitState.provider == "openai",
            ProviderCircuitState.capability == "clinical_text",
        )
    ).first()
    provider_outage = bool(circuit is not None and circuit.state != "closed")
    freshness_state: Literal["fresh", "stale", "unavailable"] = (
        "stale"
        if provider_outage or age_seconds > settings.GLANCE_STALE_AFTER_MINUTES * 60
        else "fresh"
    )
    outage_message = (
        "Remote clinical text processing is delayed; stored priorities remain visible."
        if provider_outage
        else None
    )
    fallback_kind = cast(
        Literal["stored", "rule_derived"] | None,
        "stored" if provider_outage else snapshot.fallback_kind,
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
            importance_mode=importance_mode,
            freshness_state=freshness_state,
            age_seconds=age_seconds,
            provider_outage=provider_outage,
            outage_message=outage_message,
            fallback_kind=fallback_kind,
        )
    clinical_cards = [
        ClinicalGlanceCard.model_validate(
            card | {"score_components": card.get("score_components", {})}
        )
        for card in cards[:5]
    ]
    review_cards_raw, _ = read_review_glance(snapshot)
    review_cards = [
        ClinicalGlanceCard.model_validate(
            card | {"score_components": card.get("score_components", {})}
        )
        for card in review_cards_raw
    ]
    # The review queue is server-authoritative and disjoint from the ordinary
    # top-five list. Presence in it is itself a safety-review signal, including
    # protected human-confirmed/support-review items whose decision state can
    # legitimately remain ``ready``.
    safety_review_required = bool(review_cards)
    confidence_cards = [*clinical_cards, *review_cards]
    if not confidence_cards:
        current_confidence_state = "unavailable"
    elif all(card.current_confidence_state == "qualified" for card in confidence_cards):
        current_confidence_state = "qualified"
    elif all(
        card.current_confidence_state == "unavailable" for card in confidence_cards
    ):
        current_confidence_state = "unavailable"
    else:
        current_confidence_state = "review_required"
    current_confidence_reasons = sorted(
        {
            reason
            for card in confidence_cards
            for reason in card.current_confidence_reasons
        }
    )
    return ClinicalGlancePublic(
        patient_id=patient_id,
        generated_at=generated_at,
        cards=clinical_cards,
        review_cards=review_cards,
        importance_mode=importance_mode,
        freshness_state=freshness_state,
        age_seconds=age_seconds,
        provider_outage=provider_outage,
        outage_message=outage_message,
        fallback_kind=fallback_kind,
        safety_review_required=safety_review_required,
        current_confidence_state=current_confidence_state,
        current_confidence_reasons=current_confidence_reasons,
    )
