import hashlib
import uuid

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import Session, col, select

from app.api.deps import CurrentContext, RequestContext, SessionDep
from app.core.field_crypto import field_codec
from app.models import (
    CalibrationReport,
    ClinicalFactAssertion,
    ClinicalFactAssertionPublic,
    ClinicMembership,
    ConflictCase,
    ConflictPublic,
    ConflictResolve,
    DecisionAssessment,
    DecisionExplanationPublic,
    Entry,
    EntryRelation,
    EntryVersion,
    Highlight,
    HighlightCreate,
    HighlightPublic,
    ImportanceFeedbackCreate,
    ImportanceImpression,
    ImportanceImpressionCreate,
    PatientPublication,
    PatientPublicationCreate,
    PatientPublicationItem,
    PatientPublicationPublic,
    PatientSharingRequest,
    PatientSharingRequestCreate,
    PatientSharingRequestPublic,
    ProvenancePointer,
    ProvenanceResolved,
    ReviewRequestCreate,
    User,
    get_datetime_utc,
)
from app.services.decay import lock_active_version_for_protection
from app.services.decisioning import (
    assessment_review_state,
    create_assertion,
    decision_payload,
    deterministic_risk,
    redaction_is_qualified,
)
from app.services.importance import (
    feedback_idempotency_replayed,
    lock_importance_scope,
    record_feedback,
    refresh_highlight_score,
    sanitize_feature_keys,
)
from app.services.nightingale import (
    decrypt_version,
    emit_change,
    get_scoped_entry,
    get_scoped_version,
    patch_entry,
    rebuild_glance,
    resolve_pointer,
    validate_anchor,
)

router = APIRouter(tags=["trust"])


def _require_reviewer(context: RequestContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical review role required")


def _get_highlight(
    session: Session,
    context: RequestContext,
    highlight_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Highlight:
    _require_reviewer(context)
    statement = select(Highlight).where(
        Highlight.id == highlight_id, Highlight.clinic_id == context.clinic_id
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    highlight = session.exec(statement).first()
    if highlight is None:
        raise HTTPException(status_code=404, detail="Highlight not found")
    get_scoped_entry(session, context, highlight.entry_id)
    return highlight


def _highlight_public(
    session: Session, context: RequestContext, highlight: Highlight
) -> HighlightPublic:
    pointer = session.exec(
        select(ProvenancePointer).where(
            ProvenancePointer.clinic_id == context.clinic_id,
            ProvenancePointer.highlight_id == highlight.id,
        )
    ).first()
    if pointer is None:
        raise HTTPException(status_code=500, detail="Highlight provenance missing")
    return HighlightPublic(
        id=highlight.id,
        patient_id=highlight.patient_id,
        entry_id=highlight.entry_id,
        source_entry_version_id=highlight.source_entry_version_id,
        label=field_codec.decrypt_text(
            highlight.clinic_id,
            "highlight.label",
            highlight.id,
            highlight.label_ciphertext,
        ),
        status=highlight.status,
        pinned=highlight.pinned,
        critical=highlight.critical,
        patient_facing=highlight.patient_facing,
        anchor_state=highlight.anchor_state,
        review_required=highlight.review_required,
        feature_keys=highlight.feature_keys_json,
        base_score=highlight.base_score,
        learned_score=highlight.learned_score,
        final_score=highlight.final_score,
        risk_reason=highlight.risk_reason,
        unresolved=highlight.unresolved,
        clinician_confirmed=highlight.clinician_confirmed,
        provenance_pointer_id=pointer.id,
    )


@router.post(
    "/entries/{entry_id}/highlights", response_model=HighlightPublic, status_code=201
)
def create_highlight(
    entry_id: uuid.UUID,
    body: HighlightCreate,
    session: SessionDep,
    context: CurrentContext,
) -> HighlightPublic:
    _require_reviewer(context)
    entry = get_scoped_entry(session, context, entry_id)
    version = get_scoped_version(session, context, entry, body.entry_version_id)
    # Lock the immutable source before anchor validation. Archive takes the same
    # row lock, so a protection cannot be inserted against newly-cold content.
    version = lock_active_version_for_protection(session, context, version.id)
    if body.patient_facing and not version.patient_facing:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOURCE_NOT_PATIENT_FACING",
                "message": "Publish the immutable source version before exposing it",
            },
        )
    _, content = decrypt_version(version)
    quote_hash = hashlib.sha256(body.exact_quote.encode()).hexdigest()
    anchor_state, review_required = validate_anchor(
        content,
        start_offset=body.start_offset,
        end_offset=body.end_offset,
        exact_quote=body.exact_quote,
        prefix=body.prefix,
        suffix=body.suffix,
        quote_sha256=quote_hash,
    )
    highlight_id = uuid.uuid4()
    feature_keys = sanitize_feature_keys(body.feature_keys)
    fact_type = next(
        (
            key.removeprefix("entity:")
            for key in feature_keys
            if key.startswith("entity:")
        ),
        "clinical",
    )
    risk = deterministic_risk(
        fact_type=fact_type,
        text=body.exact_quote,
        model_risk="critical" if body.critical else None,
    )
    effective_critical = risk.effective_risk == "critical"
    if effective_critical and "risk:critical" not in feature_keys:
        feature_keys.append("risk:critical")
    highlight = Highlight(
        id=highlight_id,
        clinic_id=context.clinic_id,
        patient_id=entry.patient_id,
        entry_id=entry.id,
        source_entry_version_id=version.id,
        label_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "highlight.label", highlight_id, body.label
        ),
        critical=effective_critical,
        patient_facing=body.patient_facing,
        anchor_state=anchor_state,
        review_required=review_required,
        feature_keys_json=feature_keys,
        unresolved=body.unresolved,
        clinician_confirmed=body.clinician_confirmed and context.role == "clinician",
        created_by_id=context.user_id,
    )
    session.add(highlight)
    session.flush()
    refresh_highlight_score(session, highlight)
    pointer_id = uuid.uuid4()
    pointer = ProvenancePointer(
        id=pointer_id,
        clinic_id=context.clinic_id,
        highlight_id=highlight.id,
        entry_version_id=version.id,
        start_offset=body.start_offset,
        end_offset=body.end_offset,
        exact_quote_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.exact_quote", pointer_id, body.exact_quote
        ),
        prefix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.prefix", pointer_id, body.prefix
        ),
        suffix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.suffix", pointer_id, body.suffix
        ),
        quote_sha256=quote_hash,
        anchor_state=anchor_state,
        review_required=review_required,
    )
    session.add(pointer)
    session.flush()
    assertion = create_assertion(
        session,
        clinic_id=context.clinic_id,
        patient_id=entry.patient_id,
        entry_id=entry.id,
        source_entry_version_id=version.id,
        provenance_pointer=pointer,
        fact_type=fact_type,
        subject=body.label,
        normalized_value=body.label,
        origin="human",
        highlight_id=highlight.id,
    )
    session.add(
        DecisionAssessment(
            clinic_id=context.clinic_id,
            highlight_id=highlight.id,
            assertion_id=assertion.id,
            output_type="human_asserted",
            support_state="human_asserted",
            risk_tier=risk.effective_risk,
            deterministic_floor=risk.deterministic_floor,
            model_risk=risk.model_risk,
            effective_risk=risk.effective_risk,
            risk_rule_ids_json=risk.rule_ids,
            confidence_band="not_applicable",
            abstained=False,
        )
    )
    _, affected_patients = record_feedback(
        session,
        context,
        highlight,
        signal="manual",
        idempotency_key=f"manual:create:{highlight.id}",
    )
    emit_change(
        session,
        context,
        action="highlight.created",
        resource_type="highlight",
        resource_id=highlight.id,
        metadata={"anchor_state": anchor_state, "entry_version_id": str(version.id)},
    )
    for patient_id in affected_patients | {highlight.patient_id}:
        rebuild_glance(session, context, patient_id)
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


def _transition(
    session: Session,
    context: RequestContext,
    highlight_id: uuid.UUID,
    action: str,
    idempotency_key: str | None = None,
) -> HighlightPublic:
    # Authorize before even looking up the identifier. Otherwise a patient can
    # distinguish a real internal highlight from a random UUID by observing
    # 403 versus 404, and can unnecessarily take clinical row locks.
    _require_reviewer(context)
    request_key = idempotency_key or f"{action}:{highlight_id}:{uuid.uuid4()}"
    source_version_id = session.exec(
        select(Highlight.source_entry_version_id).where(
            Highlight.id == highlight_id,
            Highlight.clinic_id == context.clinic_id,
        )
    ).first()
    if source_version_id is None:
        raise HTTPException(status_code=404, detail="Highlight not found")
    # Version must precede clinic/entry/highlight locks for every transition
    # that can dirty a protection column (including unpin on reject). This
    # matches archive's Version -> Entry -> Highlight order and cannot cycle
    # with entry edits, which take Entry -> Clinic.
    lock_active_version_for_protection(
        session,
        context,
        source_version_id,
        require_active=action in {"accept", "pin"},
    )
    lock_importance_scope(session, context.clinic_id)
    highlight = _get_highlight(session, context, highlight_id, lock=True)
    if action == "reject" and (highlight.critical or highlight.unresolved):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROTECTED_PRIORITY_REQUIRES_CLINICIAN_RESOLUTION",
                "message": "Critical and unresolved priorities stay visible until a clinician records a resolution.",
            },
        )
    if feedback_idempotency_replayed(
        session,
        context,
        highlight,
        signal=action,
        idempotency_key=request_key,
    ):
        return _highlight_public(session, context, highlight)
    if action in {"accept", "pin"} and (
        highlight.anchor_state != "resolved" or highlight.review_required
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROVENANCE_REVIEW_REQUIRED",
                "message": "Resolve the immutable source anchor before promotion",
            },
        )
    changed = False
    if action == "accept":
        changed = highlight.status != "accepted"
        highlight.status = "accepted"
    elif action == "reject":
        changed = highlight.status != "rejected" or highlight.pinned
        highlight.status = "rejected"
        highlight.pinned = False
    elif action == "pin":
        changed = not highlight.pinned
        highlight.pinned = True
    if action == "accept" and context.role == "clinician":
        changed = changed or not highlight.clinician_confirmed
        highlight.clinician_confirmed = True
    if not changed:
        return _highlight_public(session, context, highlight)
    session.add(highlight)
    _, affected_patients = record_feedback(
        session,
        context,
        highlight,
        signal=action,
        idempotency_key=request_key,
    )
    emit_change(
        session,
        context,
        action=f"highlight.{action}",
        resource_type="highlight",
        resource_id=highlight.id,
    )
    for patient_id in affected_patients | {highlight.patient_id}:
        rebuild_glance(session, context, patient_id)
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


@router.post("/highlights/{highlight_id}/accept", response_model=HighlightPublic)
def accept(
    highlight_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> HighlightPublic:
    return _transition(session, context, highlight_id, "accept", idempotency_key)


@router.post("/highlights/{highlight_id}/reject", response_model=HighlightPublic)
def reject(
    highlight_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> HighlightPublic:
    return _transition(session, context, highlight_id, "reject", idempotency_key)


@router.post("/highlights/{highlight_id}/pin", response_model=HighlightPublic)
def pin(
    highlight_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> HighlightPublic:
    return _transition(session, context, highlight_id, "pin", idempotency_key)


@router.post("/highlights/{highlight_id}/feedback", response_model=HighlightPublic)
def feedback(
    highlight_id: uuid.UUID,
    body: ImportanceFeedbackCreate,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> HighlightPublic:
    _require_reviewer(context)
    source_version_id = session.exec(
        select(Highlight.source_entry_version_id).where(
            Highlight.id == highlight_id,
            Highlight.clinic_id == context.clinic_id,
        )
    ).first()
    if source_version_id is None:
        raise HTTPException(status_code=404, detail="Highlight not found")
    lock_active_version_for_protection(
        session, context, source_version_id, require_active=False
    )
    lock_importance_scope(session, context.clinic_id)
    highlight = _get_highlight(session, context, highlight_id, lock=True)
    if highlight.critical or highlight.unresolved:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROTECTED_PRIORITY_REQUIRES_CLINICIAN_RESOLUTION",
                "message": "Critical and unresolved priorities cannot be dismissed.",
            },
        )
    if feedback_idempotency_replayed(
        session,
        context,
        highlight,
        signal=body.signal,
        idempotency_key=idempotency_key,
        reason=body.reason,
    ):
        return _highlight_public(session, context, highlight)
    if highlight.status == "dismissed" and body.reason != "too_busy_to_review":
        return _highlight_public(session, context, highlight)
    if body.reason != "too_busy_to_review":
        highlight.status = "dismissed"
        highlight.pinned = False
    session.add(highlight)
    _, affected_patients = record_feedback(
        session,
        context,
        highlight,
        signal=body.signal,
        idempotency_key=idempotency_key,
        reason=body.reason,
        learn=body.reason in {"not_relevant", "outdated"},
    )
    emit_change(
        session,
        context,
        action=f"highlight.feedback.{body.signal}.{body.reason}",
        resource_type="highlight",
        resource_id=highlight.id,
    )
    for patient_id in affected_patients | {highlight.patient_id}:
        rebuild_glance(session, context, patient_id)
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


@router.get(
    "/highlights/{highlight_id}/decision-explanation",
    response_model=DecisionExplanationPublic,
)
def decision_explanation(
    highlight_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> DecisionExplanationPublic:
    highlight = _get_highlight(session, context, highlight_id)
    assessment = session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.clinic_id == context.clinic_id,
            DecisionAssessment.highlight_id == highlight.id,
        )
    ).first()
    score = refresh_highlight_score(session, highlight)
    payload = decision_payload(
        assessment=assessment,
        highlight=highlight,
        score_components=score.components,
    )
    confidence = dict(payload["confidence"])
    if assessment and assessment.calibration_report_id:
        report = session.exec(
            select(CalibrationReport).where(
                CalibrationReport.id == assessment.calibration_report_id,
                CalibrationReport.clinic_id == context.clinic_id,
            )
        ).first()
        if report:
            confidence.update(
                {
                    "sample_count": report.sample_count,
                    "consultation_count": report.consultation_count,
                    "evaluation_set": (
                        "PriMock57 holdout"
                        if report.task == "voice_transcription"
                        else "ACI-Bench holdout"
                    ),
                    "valid_until": report.expires_at.isoformat(),
                    "metrics": report.metrics_json,
                }
            )
    return DecisionExplanationPublic(
        highlight_id=highlight.id,
        review_state=assessment_review_state(assessment, highlight),
        output_type=assessment.output_type if assessment else "human_asserted",
        support_state=assessment.support_state if assessment else "human_asserted",
        risk=payload["risk"],
        confidence=confidence,
        importance=payload["importance"],
        abstention_reason=payload["abstention_reason"],
    )


@router.post(
    "/highlights/{highlight_id}/request-review", response_model=HighlightPublic
)
def request_review(
    highlight_id: uuid.UUID,
    body: ReviewRequestCreate,
    session: SessionDep,
    context: CurrentContext,
) -> HighlightPublic:
    highlight = _get_highlight(session, context, highlight_id, lock=True)
    highlight.unresolved = True
    session.add(highlight)
    emit_change(
        session,
        context,
        action="highlight.review_requested",
        resource_type="highlight",
        resource_id=highlight.id,
        metadata={"reason": body.reason},
    )
    rebuild_glance(session, context, highlight.patient_id)
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


@router.post("/importance-impressions", status_code=204)
def record_importance_impression(
    body: ImportanceImpressionCreate,
    session: SessionDep,
    context: CurrentContext,
) -> None:
    _require_reviewer(context)
    highlight = _get_highlight(session, context, body.highlight_id)
    existing = session.exec(
        select(ImportanceImpression).where(
            ImportanceImpression.clinic_id == context.clinic_id,
            ImportanceImpression.view_event_id == body.view_event_id,
        )
    ).first()
    if existing is not None:
        return None
    session.add(
        ImportanceImpression(
            clinic_id=context.clinic_id,
            patient_id=highlight.patient_id,
            highlight_id=highlight.id,
            viewer_membership_id=context.membership.id,
            rank=body.rank,
            surface=body.surface,
            view_event_id=body.view_event_id,
            exposure_probability=body.exposure_probability,
            visible_ratio=body.visible_ratio,
            visible_duration_ms=body.visible_duration_ms,
        )
    )
    session.commit()
    return None


@router.get("/provenance/{pointer_id}/resolve", response_model=ProvenanceResolved)
def provenance_resolve(
    pointer_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> ProvenanceResolved:
    if context.role == "worker":
        raise HTTPException(
            status_code=403, detail="Role cannot resolve clinical provenance"
        )
    pointer = session.exec(
        select(ProvenancePointer).where(
            ProvenancePointer.id == pointer_id,
            ProvenancePointer.clinic_id == context.clinic_id,
        )
    ).first()
    if pointer is None:
        raise HTTPException(status_code=404, detail="Provenance not found")
    patient_highlight: Highlight | None = None
    if context.role == "patient":
        if pointer.highlight_id is None:
            raise HTTPException(status_code=404, detail="Provenance not found")
        patient_highlight = session.exec(
            select(Highlight).where(
                Highlight.id == pointer.highlight_id,
                Highlight.clinic_id == context.clinic_id,
                col(Highlight.patient_facing).is_(True),
                Highlight.source_entry_version_id == pointer.entry_version_id,
                Highlight.anchor_state == "resolved",
                col(Highlight.review_required).is_(False),
                (col(Highlight.status) == "accepted") | col(Highlight.pinned).is_(True),
            )
        ).first()
        if patient_highlight is None:
            raise HTTPException(status_code=404, detail="Provenance not found")
    version = session.exec(
        select(EntryVersion).where(
            EntryVersion.id == pointer.entry_version_id,
            EntryVersion.clinic_id == context.clinic_id,
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Provenance source not found")
    if context.role == "patient" and not version.patient_facing:
        raise HTTPException(status_code=404, detail="Provenance not found")
    entry = session.exec(
        select(Entry).where(
            Entry.id == version.entry_id, Entry.clinic_id == context.clinic_id
        )
    ).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Provenance source not found")
    if context.role == "patient" and (
        patient_highlight is None or patient_highlight.entry_id != entry.id
    ):
        raise HTTPException(status_code=404, detail="Provenance not found")
    get_scoped_entry(session, context, entry.id)
    resolved = resolve_pointer(pointer, version)
    return ProvenanceResolved.model_validate(resolved)


def _conflict_public(conflict: ConflictCase) -> ConflictPublic:
    return ConflictPublic(
        id=conflict.id,
        patient_id=conflict.patient_id,
        fact_type=conflict.fact_type,
        normalized_key=conflict.normalized_key,
        severity=conflict.severity,
        status=conflict.status,
        left_entry_id=conflict.left_entry_id,
        right_entry_id=conflict.right_entry_id,
        left_pointer_id=conflict.left_pointer_id,
        right_pointer_id=conflict.right_pointer_id,
        resolution=conflict.resolution,
        created_at=conflict.created_at,
    )


@router.get(
    "/patients/{patient_id}/clinical-facts",
    response_model=list[ClinicalFactAssertionPublic],
)
def clinical_facts_for_patient(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> list[ClinicalFactAssertionPublic]:
    """Return source-bound normalized facts for the authorized care team."""

    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Clinical team role required")
    rows = session.exec(
        select(ClinicalFactAssertion)
        .where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            ClinicalFactAssertion.patient_id == patient_id,
        )
        .order_by(
            col(ClinicalFactAssertion.effective_time).desc(),
            col(ClinicalFactAssertion.created_at).desc(),
        )
    ).all()
    return [
        ClinicalFactAssertionPublic(
            id=row.id,
            fact_type=row.fact_type,
            subject=field_codec.decrypt_text(
                row.clinic_id,
                "fact_assertion.subject",
                row.id,
                row.subject_ciphertext,
            ),
            normalized_value=field_codec.decrypt_text(
                row.clinic_id,
                "fact_assertion.normalized_value",
                row.id,
                row.normalized_value_ciphertext,
            ),
            clinical_status=row.clinical_status,
            effective_time=row.effective_time,
            origin=row.origin,
            source_entry_version_id=row.source_entry_version_id,
            provenance_pointer_id=row.provenance_pointer_id,
        )
        for row in rows
    ]


@router.get("/patients/{patient_id}/conflicts", response_model=list[ConflictPublic])
def conflicts_for_patient(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> list[ConflictPublic]:
    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Clinical team role required")
    rows = session.exec(
        select(ConflictCase)
        .where(
            ConflictCase.clinic_id == context.clinic_id,
            ConflictCase.patient_id == patient_id,
        )
        .order_by(col(ConflictCase.created_at).desc())
    ).all()
    return [_conflict_public(row) for row in rows]


@router.post("/conflicts/{conflict_id}/resolve", response_model=ConflictPublic)
def resolve_conflict(
    conflict_id: uuid.UUID,
    body: ConflictResolve,
    session: SessionDep,
    context: CurrentContext,
) -> ConflictPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician role required")
    conflict = session.exec(
        select(ConflictCase)
        .where(
            ConflictCase.id == conflict_id,
            ConflictCase.clinic_id == context.clinic_id,
        )
        .with_for_update()
    ).first()
    if conflict is None:
        raise HTTPException(status_code=404, detail="Conflict not found")
    correction = get_scoped_entry(session, context, body.correction_entry_id)
    if (
        correction.patient_id != conflict.patient_id
        or correction.section != "clinician"
        or correction.origin != "human"
    ):
        raise HTTPException(
            status_code=409, detail="Clinician correction entry required"
        )
    for target_id in {conflict.left_entry_id, conflict.right_entry_id}:
        existing = session.exec(
            select(EntryRelation).where(
                EntryRelation.clinic_id == context.clinic_id,
                EntryRelation.source_entry_id == correction.id,
                EntryRelation.target_entry_id == target_id,
                EntryRelation.relation_type == "supersedes",
            )
        ).first()
        if existing is None and target_id != correction.id:
            session.add(
                EntryRelation(
                    clinic_id=context.clinic_id,
                    source_entry_id=correction.id,
                    target_entry_id=target_id,
                    relation_type="supersedes",
                    created_by_id=context.user_id,
                )
            )
    conflict.status = "resolved"
    conflict.resolution = body.resolution
    conflict.resolved_by_membership_id = context.membership.id
    conflict.resolved_at = get_datetime_utc()
    session.add(conflict)
    emit_change(
        session,
        context,
        action="conflict.resolved",
        resource_type="conflict",
        resource_id=conflict.id,
        metadata={"correction_entry_id": str(correction.id)},
    )
    session.commit()
    session.refresh(conflict)
    return _conflict_public(conflict)


def _publication_public(
    session: Session, publication: PatientPublication
) -> PatientPublicationPublic:
    membership = session.get(ClinicMembership, publication.approved_by_membership_id)
    user = session.get(User, membership.user_id) if membership is not None else None
    items = session.exec(
        select(PatientPublicationItem).where(
            PatientPublicationItem.clinic_id == publication.clinic_id,
            PatientPublicationItem.publication_id == publication.id,
        )
    ).all()
    return PatientPublicationPublic(
        id=publication.id,
        patient_id=publication.patient_id,
        entry_version_id=publication.entry_version_id,
        approved_by_name=(user.full_name or str(user.email)) if user else "Clinician",
        approval_policy_version=publication.approval_policy_version,
        approved_at=publication.approved_at,
        withdrawn_at=publication.withdrawn_at,
        items=[
            {
                "support_state": item.support_state,
                "confidence_band": item.confidence_band,
            }
            for item in items
        ],
    )


def _sharing_request_public(
    session: Session, request: PatientSharingRequest
) -> PatientSharingRequestPublic:
    membership = session.get(ClinicMembership, request.requested_by_membership_id)
    user = session.get(User, membership.user_id) if membership else None
    return PatientSharingRequestPublic(
        id=request.id,
        patient_id=request.patient_id,
        entry_id=request.entry_id,
        entry_version_id=request.entry_version_id,
        requested_by_name=(user.full_name or str(user.email))
        if user
        else "Care team member",
        status=request.status,
        created_at=request.created_at,
        reviewed_at=request.reviewed_at,
    )


@router.post(
    "/entries/{entry_id}/patient-sharing-requests",
    response_model=PatientSharingRequestPublic,
    status_code=201,
)
def create_patient_sharing_request(
    entry_id: uuid.UUID,
    body: PatientSharingRequestCreate,
    session: SessionDep,
    context: CurrentContext,
) -> PatientSharingRequestPublic:
    _require_reviewer(context)
    entry = get_scoped_entry(session, context, entry_id)
    version = get_scoped_version(session, context, entry, body.entry_version_id)
    existing = session.exec(
        select(PatientSharingRequest).where(
            PatientSharingRequest.clinic_id == context.clinic_id,
            PatientSharingRequest.entry_version_id == version.id,
            PatientSharingRequest.status == "pending",
        )
    ).first()
    if existing is not None:
        return _sharing_request_public(session, existing)
    request = PatientSharingRequest(
        clinic_id=context.clinic_id,
        patient_id=entry.patient_id,
        entry_id=entry.id,
        entry_version_id=version.id,
        requested_by_membership_id=context.membership.id,
    )
    session.add(request)
    session.flush()
    emit_change(
        session,
        context,
        action="patient_sharing.requested",
        resource_type="patient_sharing_request",
        resource_id=request.id,
        metadata={"entry_id": str(entry.id), "version_id": str(version.id)},
    )
    session.commit()
    session.refresh(request)
    return _sharing_request_public(session, request)


@router.get(
    "/patients/{patient_id}/patient-sharing-requests",
    response_model=list[PatientSharingRequestPublic],
)
def list_patient_sharing_requests(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> list[PatientSharingRequestPublic]:
    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Clinical team role required")
    rows = session.exec(
        select(PatientSharingRequest)
        .where(
            PatientSharingRequest.clinic_id == context.clinic_id,
            PatientSharingRequest.patient_id == patient_id,
        )
        .order_by(col(PatientSharingRequest.created_at).desc())
    ).all()
    return [_sharing_request_public(session, row) for row in rows]


@router.post(
    "/entries/{entry_id}/patient-publications",
    response_model=PatientPublicationPublic,
    status_code=201,
)
def publish_for_patient(
    entry_id: uuid.UUID,
    body: PatientPublicationCreate,
    session: SessionDep,
    context: CurrentContext,
) -> PatientPublicationPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician approval required")
    entry = get_scoped_entry(session, context, entry_id)
    source = get_scoped_version(session, context, entry, body.entry_version_id)
    if entry.current_version_id != source.id:
        raise HTTPException(
            status_code=409, detail="Review the latest version before sharing"
        )
    if not redaction_is_qualified(session, clinic_id=context.clinic_id):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REDACTION_EVALUATION_REQUIRED",
                "message": "A current redaction accuracy evaluation is required before sharing.",
            },
        )
    unresolved = session.exec(
        select(ConflictCase).where(
            ConflictCase.clinic_id == context.clinic_id,
            ConflictCase.patient_id == entry.patient_id,
            ConflictCase.status == "unresolved",
            col(ConflictCase.severity).in_(["high", "critical"]),
        )
    ).first()
    if unresolved is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "UNRESOLVED_CLINICAL_CONFLICT",
                "conflict_id": str(unresolved.id),
            },
        )
    pointers = session.exec(
        select(ProvenancePointer).where(
            ProvenancePointer.clinic_id == context.clinic_id,
            ProvenancePointer.entry_version_id == source.id,
            ProvenancePointer.anchor_state == "resolved",
            col(ProvenancePointer.review_required).is_(False),
        )
    ).all()
    if not pointers:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXACT_SOURCE_REQUIRED",
                "message": "Add and verify an exact source before sharing.",
            },
        )
    if entry.origin in {"ai", "system"}:
        assessments = session.exec(
            select(DecisionAssessment)
            .join(
                Highlight,
                col(Highlight.id) == col(DecisionAssessment.highlight_id),
            )
            .where(
                DecisionAssessment.clinic_id == context.clinic_id,
                Highlight.source_entry_version_id == source.id,
            )
        ).all()
        if not assessments or any(
            item.abstained
            or item.support_state != "supported"
            or item.confidence_band not in {"high", "medium"}
            for item in assessments
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "DECISION_ASSESSMENT_NOT_PUBLISHABLE"},
            )
        pointer_highlight_ids = {pointer.highlight_id for pointer in pointers}
        if any(item.highlight_id not in pointer_highlight_ids for item in assessments):
            raise HTTPException(
                status_code=409,
                detail={"code": "CLAIM_LEVEL_PROVENANCE_REQUIRED"},
            )
    title, content = decrypt_version(source)
    updated = patch_entry(
        session,
        context,
        entry.id,
        if_match=str(source.id),
        title=title,
        content=content,
        patient_facing=True,
        action="entry.patient_sharing_approved",
        approved_patient_sharing=True,
    )
    publication = session.exec(
        select(PatientPublication).where(
            PatientPublication.clinic_id == context.clinic_id,
            PatientPublication.entry_version_id == updated.version_id,
        )
    ).first()
    if publication is None:
        raise HTTPException(status_code=500, detail="Publication record missing")
    for pointer in pointers:
        assessment = (
            session.exec(
                select(DecisionAssessment).where(
                    DecisionAssessment.clinic_id == context.clinic_id,
                    DecisionAssessment.highlight_id == pointer.highlight_id,
                )
            ).first()
            if pointer.highlight_id is not None
            else None
        )
        assertion = session.exec(
            select(ClinicalFactAssertion).where(
                ClinicalFactAssertion.clinic_id == context.clinic_id,
                ClinicalFactAssertion.provenance_pointer_id == pointer.id,
            )
        ).first()
        session.add(
            PatientPublicationItem(
                clinic_id=context.clinic_id,
                publication_id=publication.id,
                assertion_id=assertion.id if assertion else None,
                provenance_pointer_id=pointer.id,
                decision_assessment_id=assessment.id if assessment else None,
                support_state=(
                    assessment.support_state if assessment else "human_asserted"
                ),
                confidence_band=(
                    assessment.confidence_band if assessment else "not_applicable"
                ),
            )
        )
    pending_requests = session.exec(
        select(PatientSharingRequest).where(
            PatientSharingRequest.clinic_id == context.clinic_id,
            PatientSharingRequest.entry_version_id == source.id,
            PatientSharingRequest.status == "pending",
        )
    ).all()
    for request in pending_requests:
        request.status = "approved"
        request.reviewed_by_membership_id = context.membership.id
        request.reviewed_at = get_datetime_utc()
        session.add(request)
    session.commit()
    session.refresh(publication)
    return _publication_public(session, publication)


@router.post(
    "/patient-publications/{publication_id}/withdraw",
    response_model=PatientPublicationPublic,
)
def withdraw_patient_publication(
    publication_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> PatientPublicationPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician role required")
    publication = session.exec(
        select(PatientPublication).where(
            PatientPublication.id == publication_id,
            PatientPublication.clinic_id == context.clinic_id,
        )
    ).first()
    if publication is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    if publication.withdrawn_at is None:
        publication.withdrawn_at = get_datetime_utc()
        session.add(publication)
        emit_change(
            session,
            context,
            action="patient_publication.withdrawn",
            resource_type="patient_publication",
            resource_id=publication.id,
        )
        session.commit()
        session.refresh(publication)
    return _publication_public(session, publication)
