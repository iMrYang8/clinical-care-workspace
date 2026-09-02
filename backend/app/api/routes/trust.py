import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select
from starlette.responses import StreamingResponse

from app.api.deps import CurrentContext, RequestContext, SessionDep
from app.core.config import settings
from app.core.db import engine, set_rls_actor, set_rls_clinic
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
    HighlightSupportReview,
    HighlightSupportReviewPublic,
    ImportanceExposureQualificationReport,
    ImportanceExposureQualificationReportPublic,
    ImportanceExposureReportCreate,
    ImportanceExposureSurfacePublic,
    ImportanceFeedbackCreate,
    ImportanceImpression,
    ImportanceImpressionCreate,
    MedicationReviewAttestation,
    NotificationOutbox,
    PatientPortalEvent,
    PatientPortalEventPublic,
    PatientPublication,
    PatientPublicationAcknowledgement,
    PatientPublicationAcknowledgementCreate,
    PatientPublicationAcknowledgementPublic,
    PatientPublicationCorrectionCreate,
    PatientPublicationCreate,
    PatientPublicationItem,
    PatientPublicationPublic,
    PatientSharingApprovalCreate,
    PatientSharingRequest,
    PatientSharingRequestCreate,
    PatientSharingRequestPublic,
    PatientUserLink,
    ProvenancePointer,
    ProvenanceResolved,
    PublicationCorrectionOutreach,
    PublicationCorrectionOutreachPublic,
    ReviewRequestCreate,
    User,
    get_datetime_utc,
    normalize_risk_reason,
)
from app.services.clinical_formulary import screen_clinic_medication_regimen
from app.services.conflicts import recompute_highlight_conflict_state
from app.services.decay import lock_active_version_for_protection
from app.services.decisioning import (
    ai_assessment_coverage,
    assessment_review_state,
    create_assertion,
    decision_payload,
    deterministic_risk,
    public_confidence_projection,
    redaction_is_qualified,
    requalify_assessment_confidence,
)
from app.services.importance import (
    feedback_idempotency_replayed,
    generate_importance_exposure_report,
    importance_report_current_reasons,
    is_safety_protected,
    latest_importance_exposure_report,
    lock_importance_scope,
    qualify_importance_mode,
    record_feedback,
    refresh_highlight_score,
    sanitize_feature_keys,
)
from app.services.messaging import dispatch_notification, queue_notification
from app.services.nightingale import (
    decrypt_version,
    emit_change,
    get_patient,
    get_scoped_entry,
    get_scoped_version,
    patch_entry,
    rebuild_glance,
    record_patient_sharing_request,
    resolve_pointer,
    validate_anchor,
)

router = APIRouter(tags=["trust"])


def _require_reviewer(context: RequestContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical review role required")


def _require_importance_qualification_operator(context: RequestContext) -> None:
    if context.role != "clinician":
        raise HTTPException(
            status_code=403,
            detail="Clinician role required for importance qualification",
        )


def _importance_exposure_report_public(
    report: ImportanceExposureQualificationReport,
) -> ImportanceExposureQualificationReportPublic:
    current_reasons = list(importance_report_current_reasons(report))
    configured_mode = settings.IMPORTANCE_LEARNING_MODE
    effective_mode: Literal["disabled", "shadow", "active"] = configured_mode
    if configured_mode == "active" and current_reasons:
        effective_mode = "shadow"
    return ImportanceExposureQualificationReportPublic(
        id=report.id,
        report_version=report.report_version,
        window_start=report.window_start,
        window_end=report.window_end,
        source_candidate_set_count=report.source_candidate_set_count,
        candidate_count=report.candidate_count,
        telemetry_count=report.telemetry_count,
        displayed_count=report.displayed_count,
        protected_candidate_count=report.protected_candidate_count,
        protected_displayed_count=report.protected_displayed_count,
        ordinary_candidate_count=report.ordinary_candidate_count,
        ordinary_displayed_count=report.ordinary_displayed_count,
        protected_recall=report.protected_recall,
        ordinary_recall=report.ordinary_recall,
        ordinary_exposure_rate=report.ordinary_exposure_rate,
        missing_telemetry_count=report.missing_telemetry_count,
        duplicate_telemetry_count=report.duplicate_telemetry_count,
        surfaces={
            surface: ImportanceExposureSurfacePublic.model_validate(
                report.surface_metrics_json.get(surface, {})
            )
            for surface in ("current_priorities", "clinical_review")
        },
        qualified=report.qualified,
        qualification_reasons=list(report.qualification_reasons_json),
        current=not current_reasons,
        current_reasons=current_reasons,
        effective_mode=effective_mode,
        expires_at=report.expires_at,
        created_at=report.created_at,
    )


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
    assessment = session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.clinic_id == context.clinic_id,
            DecisionAssessment.highlight_id == highlight.id,
        )
    ).first()
    qualification = requalify_assessment_confidence(session, assessment)
    review_state = assessment_review_state(assessment, highlight, qualification)
    confidence_state, confidence_reasons = public_confidence_projection(qualification)
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
        risk_reason=normalize_risk_reason(highlight.risk_reason),
        unresolved=highlight.unresolved,
        clinician_confirmed=highlight.clinician_confirmed,
        provenance_pointer_id=pointer.id,
        support_state=cast(
            Literal["current", "historical", "superseded"],
            highlight.support_state,
        ),
        support_review_required=highlight.support_review_required,
        current_priority_eligible=highlight.current_priority_eligible,
        safety_review_required=(
            is_safety_protected(highlight) or review_state != "ready"
        ),
        current_confidence_state=confidence_state,
        current_confidence_reasons=confidence_reasons,
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
    ai_derived = entry.origin in {"ai", "system"} or entry.entry_type.startswith("ai_")
    if ai_derived and context.role != "clinician":
        # Selecting wording from a derived note is a clinical confirmation,
        # not ordinary care-team feedback. Enforce the same authority at the
        # API boundary as the clinician-only browser control so a Staff token
        # cannot turn uncalibrated AI text into a human assertion.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CLINICIAN_CONFIRMATION_REQUIRED",
                "message": "A clinician must confirm highlights from AI-assisted notes.",
            },
        )
    if ai_derived and " ".join(body.label.split()) != " ".join(
        body.exact_quote.split()
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "EXACT_SOURCE_WORDING_REQUIRED",
                "message": "An AI-assisted priority must use the selected source wording. Record a separate clinical correction to paraphrase it.",
            },
        )
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
    ai_confirmation_accepted = (
        ai_derived
        and context.role == "clinician"
        and anchor_state == "resolved"
        and not review_required
    )
    highlight = Highlight(
        id=highlight_id,
        clinic_id=context.clinic_id,
        patient_id=entry.patient_id,
        entry_id=entry.id,
        source_entry_version_id=version.id,
        label_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "highlight.label", highlight_id, body.label
        ),
        status="accepted" if ai_confirmation_accepted else "pending",
        critical=effective_critical,
        patient_facing=body.patient_facing,
        anchor_state=anchor_state,
        review_required=review_required,
        feature_keys_json=feature_keys,
        unresolved=body.unresolved,
        clinician_confirmed=(
            context.role == "clinician" and (ai_derived or body.clinician_confirmed)
        ),
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
    if action == "reject" and is_safety_protected(highlight):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROTECTED_PRIORITY_REQUIRES_CLINICIAN_RESOLUTION",
                "message": "Safety-protected priorities stay visible until a clinician records a correction or resolution.",
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


def _review_highlight_support(
    session: Session,
    context: RequestContext,
    highlight_id: uuid.UUID,
    *,
    supersede: bool,
) -> HighlightPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician review required")
    highlight = _get_highlight(session, context, highlight_id, lock=True)
    review = session.exec(
        select(HighlightSupportReview)
        .where(
            HighlightSupportReview.clinic_id == context.clinic_id,
            HighlightSupportReview.highlight_id == highlight.id,
            HighlightSupportReview.review_status == "pending",
        )
        .order_by(col(HighlightSupportReview.created_at).desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if review is None:
        if not highlight.support_review_required:
            return _highlight_public(session, context, highlight)
        raise HTTPException(status_code=409, detail={"code": "SUPPORT_REVIEW_MISSING"})
    reviewed_at = get_datetime_utc()
    obsolete_pending = session.exec(
        select(HighlightSupportReview)
        .where(
            HighlightSupportReview.clinic_id == context.clinic_id,
            HighlightSupportReview.highlight_id == highlight.id,
            HighlightSupportReview.review_status == "pending",
            HighlightSupportReview.id != review.id,
        )
        .with_for_update()
    ).all()
    for obsolete in obsolete_pending:
        obsolete.review_status = "superseded"
        obsolete.reviewed_by_membership_id = context.membership.id
        obsolete.reviewed_at = reviewed_at
        session.add(obsolete)
    review.review_status = "superseded" if supersede else "reaffirmed"
    review.support_state = "superseded" if supersede else "historical"
    review.reviewed_by_membership_id = context.membership.id
    review.reviewed_at = reviewed_at
    highlight.support_state = review.support_state
    highlight.support_review_required = False
    highlight.current_priority_eligible = not supersede
    if supersede:
        highlight.pinned = False
    session.add(review)
    session.add(highlight)
    emit_change(
        session,
        context,
        action=(
            "highlight.support_superseded"
            if supersede
            else "highlight.support_reaffirmed"
        ),
        resource_type="highlight",
        resource_id=highlight.id,
        metadata={"support_review_id": str(review.id)},
    )
    rebuild_glance(session, context, highlight.patient_id)
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


@router.post(
    "/highlights/{highlight_id}/support-review/reaffirm",
    response_model=HighlightPublic,
)
def reaffirm_highlight_support(
    highlight_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> HighlightPublic:
    return _review_highlight_support(session, context, highlight_id, supersede=False)


@router.post(
    "/highlights/{highlight_id}/support-review/supersede",
    response_model=HighlightPublic,
)
def supersede_highlight_support(
    highlight_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> HighlightPublic:
    return _review_highlight_support(session, context, highlight_id, supersede=True)


@router.get(
    "/patients/{patient_id}/highlight-support-reviews",
    response_model=list[HighlightSupportReviewPublic],
)
def list_highlight_support_reviews(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> list[HighlightSupportReviewPublic]:
    _require_reviewer(context)
    get_patient(session, context, patient_id)
    rows = session.exec(
        select(HighlightSupportReview)
        .where(
            HighlightSupportReview.clinic_id == context.clinic_id,
            HighlightSupportReview.patient_id == patient_id,
        )
        .order_by(col(HighlightSupportReview.created_at).desc())
    ).all()
    return [HighlightSupportReviewPublic.model_validate(item) for item in rows]


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
    if is_safety_protected(highlight) and body.reason != "too_busy_to_review":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROTECTED_PRIORITY_REQUIRES_CLINICIAN_RESOLUTION",
                "message": "Safety-protected priorities cannot be dismissed through ranking feedback.",
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
    qualification = requalify_assessment_confidence(session, assessment)
    confidence_state, confidence_reasons = public_confidence_projection(qualification)
    review_state = assessment_review_state(assessment, highlight, qualification)
    payload = decision_payload(
        assessment=assessment,
        highlight=highlight,
        score_components=score.components,
        confidence_qualification=qualification,
        importance_mode=qualify_importance_mode(
            session, context.clinic_id
        ).effective_mode,
    )
    confidence = dict(payload["confidence"])
    if assessment and assessment.calibration_report_id:
        report = session.exec(
            select(CalibrationReport).where(
                CalibrationReport.id == assessment.calibration_report_id,
                CalibrationReport.clinic_id == context.clinic_id,
            )
        ).first()
        if report and qualification.qualified:
            confidence.update(
                {
                    "sample_count": report.sample_count,
                    "total_sample_count": report.total_sample_count,
                    "calibration_sample_count": report.calibration_sample_count,
                    "holdout_sample_count": report.holdout_sample_count,
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
        review_state=review_state,
        output_type=assessment.output_type if assessment else "human_asserted",
        support_state=assessment.support_state if assessment else "human_asserted",
        risk=payload["risk"],
        confidence=confidence,
        importance=payload["importance"],
        abstention_reason=payload["abstention_reason"],
        current_confidence_state=confidence_state,
        confidence_qualification_reasons=confidence_reasons,
        confidence_qualified_at=get_datetime_utc(),
        safety_review_required=(
            review_state != "ready" or is_safety_protected(highlight)
        ),
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
        reason_code="clinical_review_requested",
        clinical_rationale=body.reason,
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


@router.post(
    "/importance/exposure-reports",
    response_model=ImportanceExposureQualificationReportPublic,
    status_code=201,
)
def create_importance_exposure_report(
    body: ImportanceExposureReportCreate,
    session: SessionDep,
    context: CurrentContext,
) -> ImportanceExposureQualificationReportPublic:
    _require_importance_qualification_operator(context)
    report = generate_importance_exposure_report(
        session,
        clinic_id=context.clinic_id,
        generated_by_membership_id=context.membership.id,
        window_hours=body.window_hours,
    )
    session.commit()
    session.refresh(report)
    return _importance_exposure_report_public(report)


@router.get(
    "/importance/exposure-reports/current",
    response_model=ImportanceExposureQualificationReportPublic,
)
def current_importance_exposure_report(
    session: SessionDep,
    context: CurrentContext,
) -> ImportanceExposureQualificationReportPublic:
    _require_importance_qualification_operator(context)
    report = latest_importance_exposure_report(session, context.clinic_id)
    if report is None:
        raise HTTPException(
            status_code=404, detail="Importance exposure report not found"
        )
    return _importance_exposure_report_public(report)


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
        active_publication = session.exec(
            select(PatientPublication).where(
                PatientPublication.clinic_id == context.clinic_id,
                PatientPublication.patient_id == patient_highlight.patient_id,
                PatientPublication.entry_id == patient_highlight.entry_id,
                PatientPublication.entry_version_id == pointer.entry_version_id,
                col(PatientPublication.withdrawn_at).is_(None),
            )
        ).first()
        if active_publication is None:
            # A compound approval may publish a new patient-facing projection
            # while retaining the requested immutable source version in its
            # item list. That exact binding is also valid; an unrelated pointer
            # from a withdrawn historical version is not.
            active_publication = session.exec(
                select(PatientPublication)
                .join(
                    PatientPublicationItem,
                    col(PatientPublicationItem.publication_id)
                    == col(PatientPublication.id),
                )
                .where(
                    PatientPublication.clinic_id == context.clinic_id,
                    PatientPublication.patient_id == patient_highlight.patient_id,
                    col(PatientPublication.withdrawn_at).is_(None),
                    PatientPublicationItem.provenance_pointer_id == pointer.id,
                )
            ).first()
        if active_publication is None:
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
    if pointer.highlight_id is not None:
        source_highlight = session.exec(
            select(Highlight).where(
                Highlight.id == pointer.highlight_id,
                Highlight.clinic_id == context.clinic_id,
            )
        ).first()
        if source_highlight is not None:
            resolved["support_state"] = source_highlight.support_state
    return ProvenanceResolved.model_validate(resolved)


def _conflict_public(session: Session, conflict: ConflictCase) -> ConflictPublic:
    left = session.get(ClinicalFactAssertion, conflict.left_assertion_id)
    right = session.get(ClinicalFactAssertion, conflict.right_assertion_id)
    assertion_ids = [
        assertion_id
        for assertion_id in (conflict.left_assertion_id, conflict.right_assertion_id)
        if assertion_id is not None
    ]
    assessments = (
        session.exec(
            select(DecisionAssessment).where(
                DecisionAssessment.clinic_id == conflict.clinic_id,
                col(DecisionAssessment.assertion_id).in_(assertion_ids),
            )
        ).all()
        if assertion_ids
        else []
    )
    confidence_reasons: list[str] = []
    expected_ai_assertion_ids = {
        assertion.id
        for assertion in (left, right)
        if assertion is not None and assertion.origin in {"ai", "system"}
    }
    assessed_ai_assertion_ids = {
        assessment.assertion_id
        for assessment in assessments
        if assessment.assertion_id in expected_ai_assertion_ids
    }
    missing_ai_assessment = bool(expected_ai_assertion_ids - assessed_ai_assertion_ids)
    confidence_state: Literal["qualified", "unavailable", "review_required"]
    confidence_review_required = False
    if assessments:
        qualifications = [
            requalify_assessment_confidence(session, assessment)
            for assessment in assessments
        ]
        confidence_reasons.extend(
            reason
            for qualification in qualifications
            for reason in qualification.reasons
        )
        abstention_reasons = [
            assessment.abstention_reason or "ASSESSMENT_ABSTAINED_REVIEW_REQUIRED"
            for assessment in assessments
            if assessment.abstained
        ]
        confidence_reasons.extend(abstention_reasons)
        confidence_review_required = bool(
            any(not qualification.qualified for qualification in qualifications)
            or abstention_reasons
            or missing_ai_assessment
        )
        if missing_ai_assessment:
            confidence_reasons.append("CONFLICT_CONFIDENCE_ASSESSMENT_UNAVAILABLE")
        confidence_state = (
            "review_required" if confidence_review_required else "qualified"
        )
    elif expected_ai_assertion_ids:
        confidence_state = "review_required"
        confidence_review_required = True
        confidence_reasons.append("CONFLICT_CONFIDENCE_ASSESSMENT_UNAVAILABLE")
    else:
        confidence_state = "unavailable"
        confidence_reasons.append("CONFLICT_CONFIDENCE_NOT_APPLICABLE")

    missing_evidence = left is None or right is None
    if missing_evidence:
        confidence_review_required = True
        confidence_state = "review_required"
        confidence_reasons.append("CONFLICT_ASSERTION_EVIDENCE_UNAVAILABLE")
    if conflict.status == "unresolved":
        safety_review_state: Literal[
            "ready", "review_required", "critical_unresolved"
        ] = "critical_unresolved"
    elif confidence_review_required:
        safety_review_state = "review_required"
    else:
        safety_review_state = "ready"
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
        left_assertion_scope=left.assertion_scope if left is not None else None,
        right_assertion_scope=right.assertion_scope if right is not None else None,
        left_polarity=left.polarity if left is not None else None,
        right_polarity=right.polarity if right is not None else None,
        left_allergy_category=(
            cast(
                Literal["drug", "food", "environmental"],
                left.allergy_category,
            )
            if left is not None and left.allergy_category is not None
            else None
        ),
        right_allergy_category=(
            cast(
                Literal["drug", "food", "environmental"],
                right.allergy_category,
            )
            if right is not None and right.allergy_category is not None
            else None
        ),
        left_origin=left.origin if left is not None else None,
        right_origin=right.origin if right is not None else None,
        left_source_role=left.source_role if left is not None else None,
        right_source_role=right.source_role if right is not None else None,
        left_source_section=left.source_section if left is not None else None,
        right_source_section=right.source_section if right is not None else None,
        left_source_language=left.source_language if left is not None else None,
        right_source_language=right.source_language if right is not None else None,
        left_assertion_state=(
            cast(Literal["active", "superseded"], left.assertion_state)
            if left is not None
            else None
        ),
        right_assertion_state=(
            cast(Literal["active", "superseded"], right.assertion_state)
            if right is not None
            else None
        ),
        left_effective_time=left.effective_time if left is not None else None,
        right_effective_time=right.effective_time if right is not None else None,
        left_recorded_at=left.created_at if left is not None else None,
        right_recorded_at=right.created_at if right is not None else None,
        review_required=safety_review_state != "ready",
        safety_review_state=safety_review_state,
        current_confidence_state=confidence_state,
        current_confidence_reasons=sorted(set(confidence_reasons)),
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
            polarity=row.polarity,
            assertion_scope=row.assertion_scope,
            allergy_category=(
                cast(
                    Literal["drug", "food", "environmental"],
                    row.allergy_category,
                )
                if row.allergy_category is not None
                else None
            ),
            source_language=row.source_language,
            source_role=row.source_role,
            source_section=row.source_section,
            assertion_state=cast(Literal["active", "superseded"], row.assertion_state),
            superseded_by_assertion_id=row.superseded_by_assertion_id,
            superseded_at=row.superseded_at,
            clinical_status=row.clinical_status,
            effective_time=row.effective_time,
            origin=row.origin,
            source_entry_version_id=row.source_entry_version_id,
            provenance_pointer_id=row.provenance_pointer_id,
            medication=(
                field_codec.decrypt_text(
                    row.clinic_id,
                    "fact_assertion.medication",
                    row.id,
                    row.medication_ciphertext,
                )
                if row.medication_ciphertext is not None
                else None
            ),
            dose_value=row.dose_value,
            dose_unit=row.dose_unit,
            route=row.route,
            frequency=row.frequency,
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
    return [_conflict_public(session, row) for row in rows]


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
    if conflict.status != "unresolved":
        raise HTTPException(
            status_code=409,
            detail={"code": "CONFLICT_NOT_ACTIVE", "status": conflict.status},
        )
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
    linked_assertions = session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            col(ClinicalFactAssertion.id).in_(
                [conflict.left_assertion_id, conflict.right_assertion_id]
            ),
        )
    ).all()
    linked_highlight_ids = {
        assertion.highlight_id
        for assertion in linked_assertions
        if assertion.highlight_id is not None
    }
    session.flush()
    affected_patients = recompute_highlight_conflict_state(
        session,
        context,
        linked_highlight_ids,
    )
    emit_change(
        session,
        context,
        action="conflict.resolved",
        resource_type="conflict",
        resource_id=conflict.id,
        metadata={"correction_entry_id": str(correction.id)},
    )
    for patient_id in affected_patients | {conflict.patient_id}:
        rebuild_glance(session, context, patient_id)
    session.commit()
    session.refresh(conflict)
    return _conflict_public(session, conflict)


def _publication_public(
    session: Session, publication: PatientPublication
) -> PatientPublicationPublic:
    version = session.get(EntryVersion, publication.entry_version_id)
    if version is None or version.clinic_id != publication.clinic_id:
        raise HTTPException(status_code=500, detail="Publication source missing")
    entry = session.get(Entry, version.entry_id)
    if entry is None or entry.clinic_id != publication.clinic_id:
        raise HTTPException(status_code=500, detail="Publication entry missing")
    title, _ = decrypt_version(version)
    membership = session.get(ClinicMembership, publication.approved_by_membership_id)
    user = session.get(User, membership.user_id) if membership is not None else None
    items = session.exec(
        select(PatientPublicationItem).where(
            PatientPublicationItem.clinic_id == publication.clinic_id,
            PatientPublicationItem.publication_id == publication.id,
        )
    ).all()
    replacement = session.exec(
        select(PatientPublication).where(
            PatientPublication.clinic_id == publication.clinic_id,
            PatientPublication.supersedes_publication_id == publication.id,
        )
    ).first()
    acknowledgement_publication_ids = [publication.id]
    if publication.supersedes_publication_id is not None:
        acknowledgement_publication_ids.append(publication.supersedes_publication_id)
    acknowledgement = session.exec(
        select(PatientPublicationAcknowledgement).where(
            PatientPublicationAcknowledgement.clinic_id == publication.clinic_id,
            col(PatientPublicationAcknowledgement.publication_id).in_(
                acknowledgement_publication_ids
            ),
            PatientPublicationAcknowledgement.event_type == "acknowledged",
        )
    ).first()
    outreach = session.exec(
        select(PublicationCorrectionOutreach)
        .where(
            PublicationCorrectionOutreach.clinic_id == publication.clinic_id,
            (PublicationCorrectionOutreach.withdrawn_publication_id == publication.id)
            | (
                PublicationCorrectionOutreach.replacement_publication_id
                == publication.id
            ),
        )
        .order_by(col(PublicationCorrectionOutreach.created_at).desc())
    ).first()
    notification: NotificationOutbox | None = None
    if outreach is not None and outreach.notification_id is not None:
        candidate = session.get(NotificationOutbox, outreach.notification_id)
        if candidate is not None and candidate.clinic_id == publication.clinic_id:
            notification = candidate
    if notification is None:
        notification = session.exec(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.clinic_id == publication.clinic_id,
                NotificationOutbox.publication_id == publication.id,
                NotificationOutbox.purpose == "correction",
            )
            .order_by(col(NotificationOutbox.created_at).desc())
        ).first()
    delivery_warning: (
        Literal[
            "notification_queue_failed",
            "notification_delivery_failed",
            "notification_revoked",
        ]
        | None
    ) = None
    if outreach is not None and outreach.notification_id is None:
        delivery_warning = "notification_queue_failed"
    elif notification is not None and notification.state == "failed":
        delivery_warning = "notification_delivery_failed"
    elif notification is not None and notification.state == "revoked":
        delivery_warning = "notification_revoked"
    acknowledgement_state = "not_required"
    if acknowledgement is not None:
        acknowledgement_state = "acknowledged"
    elif outreach is not None:
        acknowledgement_state = "pending"
    elif replacement is not None or publication.supersedes_publication_id is not None:
        acknowledgement_state = "pending"
    return PatientPublicationPublic(
        id=publication.id,
        patient_id=publication.patient_id,
        entry_id=entry.id,
        entry_version_id=publication.entry_version_id,
        supersedes_publication_id=publication.supersedes_publication_id,
        entry_title=title,
        approved_by_name=(user.full_name or str(user.email)) if user else "Clinician",
        approval_policy_version=publication.approval_policy_version,
        approved_at=publication.approved_at,
        withdrawn_at=publication.withdrawn_at,
        medication_review_complete=publication.medication_review_complete,
        medication_reviews=publication.medication_review_json,
        correction_reason_code=publication.correction_reason_code,
        replacement_publication_id=replacement.id if replacement is not None else None,
        acknowledgement_state=acknowledgement_state,
        outreach_required=bool(outreach is not None and outreach.status == "pending"),
        notification_id=notification.id if notification is not None else None,
        notification_state=cast(
            Literal[
                "queued",
                "submitted",
                "delivered",
                "failed",
                "acknowledged",
                "revoked",
            ]
            | None,
            notification.state if notification is not None else None,
        ),
        delivery_warning=delivery_warning,
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
    reviewer_membership = (
        session.get(ClinicMembership, request.reviewed_by_membership_id)
        if request.reviewed_by_membership_id is not None
        else None
    )
    reviewer = (
        session.get(User, reviewer_membership.user_id)
        if reviewer_membership is not None
        else None
    )
    entry = session.get(Entry, request.entry_id)
    version = session.get(EntryVersion, request.entry_version_id)
    if (
        entry is None
        or version is None
        or entry.clinic_id != request.clinic_id
        or version.clinic_id != request.clinic_id
    ):
        raise HTTPException(status_code=500, detail="Sharing request source missing")
    title, _ = decrypt_version(version)
    return PatientSharingRequestPublic(
        id=request.id,
        patient_id=request.patient_id,
        entry_id=request.entry_id,
        entry_version_id=request.entry_version_id,
        entry_title=title,
        entry_section=entry.section,
        entry_origin=entry.origin,
        requested_by_name=(user.full_name or str(user.email))
        if user
        else "Care team member",
        status=request.status,
        created_at=request.created_at,
        reviewed_at=request.reviewed_at,
        reviewed_by_name=(
            reviewer.full_name or str(reviewer.email) if reviewer else None
        ),
        publication_id=request.publication_id,
    )


def _whole_version_pointer(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    version: EntryVersion,
    content: str,
) -> ProvenancePointer:
    """Return a resolved immutable pointer covering the approved human note."""

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    existing = session.exec(
        select(ProvenancePointer).where(
            ProvenancePointer.clinic_id == clinic_id,
            ProvenancePointer.entry_version_id == version.id,
            ProvenancePointer.start_offset == 0,
            ProvenancePointer.end_offset == len(content),
            ProvenancePointer.quote_sha256 == content_hash,
            col(ProvenancePointer.highlight_id).is_(None),
            col(ProvenancePointer.comment_id).is_(None),
            ProvenancePointer.anchor_state == "resolved",
            col(ProvenancePointer.review_required).is_(False),
        )
    ).first()
    if existing is not None:
        return existing
    pointer_id = uuid.uuid4()
    pointer = ProvenancePointer(
        id=pointer_id,
        clinic_id=clinic_id,
        entry_version_id=version.id,
        start_offset=0,
        end_offset=len(content),
        exact_quote_ciphertext=field_codec.encrypt_text(
            clinic_id, "provenance.exact_quote", pointer_id, content
        ),
        prefix_ciphertext=field_codec.encrypt_text(
            clinic_id, "provenance.prefix", pointer_id, ""
        ),
        suffix_ciphertext=field_codec.encrypt_text(
            clinic_id, "provenance.suffix", pointer_id, ""
        ),
        quote_sha256=content_hash,
    )
    session.add(pointer)
    session.flush()
    return pointer


def _normalize_medication_text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.casefold().split())


def _normalize_regimen_value(value: str | None, *, kind: str) -> str | None:
    normalized = _normalize_medication_text(value)
    if normalized is None:
        return None
    normalized = " ".join(normalized.replace("_", " ").split())
    if kind == "route":
        return {
            "po": "oral",
            "iv": "intravenous",
            "im": "intramuscular",
            "sc": "subcutaneous",
        }.get(normalized, normalized)
    if kind == "frequency":
        return {
            "qd": "once daily",
            "daily": "once daily",
            "bid": "twice daily",
            "tid": "three times daily",
            "qid": "four times daily",
        }.get(normalized, normalized)
    return normalized


def _validated_medication_reviews(
    session: Session,
    context: RequestContext,
    source: EntryVersion,
    reviews: list[MedicationReviewAttestation],
) -> list[dict[str, object]]:
    """Require an exact clinician attestation for every complete regimen field."""

    assertions = session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            ClinicalFactAssertion.source_entry_version_id == source.id,
            ClinicalFactAssertion.assertion_state == "active",
            col(ClinicalFactAssertion.fact_type).in_(
                ["medication", "dose", "route", "frequency"]
            ),
        )
    ).all()
    if not assertions:
        if reviews:
            raise HTTPException(
                status_code=409,
                detail={"code": "MEDICATION_REVIEW_SOURCE_MISMATCH"},
            )
        return []

    regimens: dict[str, dict[str, object]] = {}
    for assertion in assertions:
        if assertion.medication_ciphertext is None:
            continue
        medication = field_codec.decrypt_text(
            assertion.clinic_id,
            "fact_assertion.medication",
            assertion.id,
            assertion.medication_ciphertext,
        )
        key = _normalize_medication_text(medication) or ""
        regimen = regimens.setdefault(
            key,
            {
                "medication": medication,
                "assertion_ids": set(),
                "dose_value": None,
                "dose_unit": None,
                "route": None,
                "frequency": None,
            },
        )
        assertion_ids = cast(set[uuid.UUID], regimen["assertion_ids"])
        assertion_ids.add(assertion.id)
        if assertion.dose_value is not None:
            regimen["dose_value"] = assertion.dose_value
            regimen["dose_unit"] = assertion.dose_unit
        if assertion.route is not None:
            regimen["route"] = assertion.route
        if assertion.frequency is not None:
            regimen["frequency"] = assertion.frequency

    if not regimens:
        return []
    if len(reviews) != len(regimens):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MEDICATION_REVIEW_REQUIRED",
                "message": "Confirm medication, dose, unit, route, and frequency before publication.",
            },
        )
    serialized: list[dict[str, object]] = []
    seen: set[str] = set()
    source_entry = session.get(Entry, source.entry_id)
    if source_entry is None or source_entry.clinic_id != context.clinic_id:
        raise HTTPException(status_code=500, detail="Medication source entry missing")
    active_allergy_concepts = [
        field_codec.decrypt_text(
            assertion.clinic_id,
            "fact_assertion.subject",
            assertion.id,
            assertion.subject_ciphertext,
        )
        for assertion in session.exec(
            select(ClinicalFactAssertion).where(
                ClinicalFactAssertion.clinic_id == context.clinic_id,
                ClinicalFactAssertion.patient_id == source_entry.patient_id,
                ClinicalFactAssertion.fact_type == "allergy",
                ClinicalFactAssertion.assertion_state == "active",
                ClinicalFactAssertion.polarity == "present",
            )
        ).all()
    ]
    for review in reviews:
        key = _normalize_medication_text(review.medication) or ""
        expected = regimens.get(key)
        if expected is None or key in seen or not review.confirmed:
            raise HTTPException(
                status_code=409,
                detail={"code": "MEDICATION_REVIEW_MISMATCH"},
            )
        seen.add(key)
        required_values = (
            expected["dose_value"],
            expected["dose_unit"],
            expected["route"],
            expected["frequency"],
        )
        if any(value is None for value in required_values):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MEDICATION_REGIMEN_INCOMPLETE",
                    "medication": expected["medication"],
                },
            )
        assertion_ids = cast(set[uuid.UUID], expected["assertion_ids"])
        matches = (
            review.assertion_id in assertion_ids
            and review.dose_value is not None
            and abs(review.dose_value - float(str(expected["dose_value"]))) < 1e-9
            and _normalize_regimen_value(review.dose_unit, kind="unit")
            == _normalize_regimen_value(str(expected["dose_unit"]), kind="unit")
            and _normalize_regimen_value(review.route, kind="route")
            == _normalize_regimen_value(str(expected["route"]), kind="route")
            and _normalize_regimen_value(review.frequency, kind="frequency")
            == _normalize_regimen_value(str(expected["frequency"]), kind="frequency")
        )
        if not matches:
            raise HTTPException(
                status_code=409,
                detail={"code": "MEDICATION_REVIEW_MISMATCH"},
            )
        screening = screen_clinic_medication_regimen(
            session,
            clinic_id=context.clinic_id,
            medication=review.medication,
            dose_value=review.dose_value,
            dose_unit=review.dose_unit,
            route=review.route,
            frequency=review.frequency,
            active_allergy_concepts=active_allergy_concepts,
        )
        if not screening.eligible:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MEDICATION_FORMULARY_REVIEW_REQUIRED",
                    "formulary_version": screening.formulary_version,
                    "reason_codes": list(screening.reason_codes),
                },
            )
        serialized.append(
            review.model_dump(mode="json")
            | {
                "formulary_version": screening.formulary_version,
                "formulary_qualification_source": screening.qualification_source,
            }
        )
    return serialized


def _bind_medication_reviews_to_published_version(
    session: Session,
    context: RequestContext,
    source: EntryVersion,
    published: EntryVersion,
    reviews: list[MedicationReviewAttestation],
) -> list[dict[str, object]]:
    """Rebind an attestation to the immutable version actually published.

    Patient sharing creates a byte-identical, patient-facing version after the
    clinician approves the current private source. Conflict extraction also
    recreates the source-bound assertion rows for that new version. Keeping the
    submitted assertion id would therefore leave the publication pointing at a
    now-historical assertion even though the reviewed words are identical.

    Verify the immutable content digest, map each medication to the regenerated
    medication assertion, and run the full regimen validator again against the
    publication version. Any parser drift or incomplete regenerated regimen
    fails closed inside the same transaction.
    """

    if source.content_sha256 != published.content_sha256:
        raise HTTPException(
            status_code=409,
            detail={"code": "MEDICATION_REVIEW_SOURCE_CHANGED"},
        )
    if not reviews:
        return _validated_medication_reviews(session, context, published, [])

    published_assertions = session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            ClinicalFactAssertion.source_entry_version_id == published.id,
            ClinicalFactAssertion.assertion_state == "active",
            ClinicalFactAssertion.fact_type == "medication",
            col(ClinicalFactAssertion.medication_ciphertext).is_not(None),
        )
    ).all()
    assertion_by_medication: dict[str, ClinicalFactAssertion] = {}
    for assertion in published_assertions:
        assert assertion.medication_ciphertext is not None
        medication = field_codec.decrypt_text(
            assertion.clinic_id,
            "fact_assertion.medication",
            assertion.id,
            assertion.medication_ciphertext,
        )
        key = _normalize_medication_text(medication) or ""
        if key in assertion_by_medication:
            raise HTTPException(
                status_code=409,
                detail={"code": "MEDICATION_REGIMEN_AMBIGUOUS"},
            )
        assertion_by_medication[key] = assertion

    rebound: list[MedicationReviewAttestation] = []
    for review in reviews:
        key = _normalize_medication_text(review.medication) or ""
        published_assertion = assertion_by_medication.get(key)
        if published_assertion is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "MEDICATION_REVIEW_SOURCE_CHANGED"},
            )
        rebound.append(
            review.model_copy(update={"assertion_id": published_assertion.id})
        )
    return _validated_medication_reviews(session, context, published, rebound)


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
    if context.role != "staff":
        raise HTTPException(status_code=403, detail="Care staff request required")
    entry = get_scoped_entry(session, context, entry_id, lock=True)
    version = get_scoped_version(session, context, entry, body.entry_version_id)
    if entry.section != "staff" or entry.origin != "human":
        raise HTTPException(
            status_code=409,
            detail="Only a care staff note can enter the sharing review queue",
        )
    existing = session.exec(
        select(PatientSharingRequest)
        .where(
            PatientSharingRequest.clinic_id == context.clinic_id,
            PatientSharingRequest.patient_id == entry.patient_id,
            PatientSharingRequest.entry_id == entry.id,
            PatientSharingRequest.entry_version_id == version.id,
            PatientSharingRequest.status == "pending",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if existing is not None:
        return _sharing_request_public(session, existing)
    request = record_patient_sharing_request(
        session,
        context,
        entry,
        version,
    )
    emit_change(
        session,
        context,
        action="entry.patient_sharing_requested",
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
    get_patient(session, context, patient_id)
    rows = session.exec(
        select(PatientSharingRequest)
        .where(
            PatientSharingRequest.clinic_id == context.clinic_id,
            PatientSharingRequest.patient_id == patient_id,
        )
        .order_by(col(PatientSharingRequest.created_at).desc())
    ).all()
    return [_sharing_request_public(session, row) for row in rows]


@router.get(
    "/patients/{patient_id}/patient-publications",
    response_model=list[PatientPublicationPublic],
)
def list_patient_publications(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> list[PatientPublicationPublic]:
    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Clinical team role required")
    get_patient(session, context, patient_id)
    rows = session.exec(
        select(PatientPublication)
        .where(
            PatientPublication.clinic_id == context.clinic_id,
            PatientPublication.patient_id == patient_id,
        )
        .order_by(col(PatientPublication.approved_at).desc())
    ).all()
    return [_publication_public(session, row) for row in rows]


def _publish_for_patient(
    entry_id: uuid.UUID,
    body: PatientPublicationCreate,
    session: Session,
    context: RequestContext,
    *,
    commit: bool,
) -> PatientPublicationPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician approval required")
    # Entry is the lock-order root shared by edits, review requests and
    # publications. It also refreshes current_version_id before any decision.
    entry = get_scoped_entry(session, context, entry_id, lock=True)
    source = get_scoped_version(session, context, entry, body.entry_version_id)
    sharing_request: PatientSharingRequest | None = None
    if (
        entry.origin == "human"
        and entry.section == "staff"
        and body.sharing_request_id is None
        and body.correction_reason_code is None
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STAFF_SHARING_REQUEST_REQUIRED",
                "message": "Care staff must submit the saved note for clinician review before publication.",
            },
        )
    if body.sharing_request_id is not None:
        sharing_request = session.exec(
            select(PatientSharingRequest)
            .where(
                PatientSharingRequest.id == body.sharing_request_id,
                PatientSharingRequest.clinic_id == context.clinic_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if sharing_request is None:
            raise HTTPException(status_code=404, detail="Sharing request not found")
        if (
            sharing_request.entry_id != entry.id
            or sharing_request.entry_version_id != source.id
            or sharing_request.patient_id != entry.patient_id
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "SHARING_REQUEST_SOURCE_MISMATCH"},
            )
        if sharing_request.status == "approved" and sharing_request.publication_id:
            replay = session.exec(
                select(PatientPublication).where(
                    PatientPublication.clinic_id == context.clinic_id,
                    PatientPublication.patient_id == entry.patient_id,
                    PatientPublication.entry_id == entry.id,
                    PatientPublication.id == sharing_request.publication_id,
                    col(PatientPublication.withdrawn_at).is_(None),
                )
            ).first()
            if replay is not None:
                return _publication_public(session, replay)
        if sharing_request.status != "pending":
            raise HTTPException(
                status_code=409,
                detail={"code": "SHARING_REQUEST_ALREADY_REVIEWED"},
            )
    if entry.current_version_id != source.id:
        raise HTTPException(
            status_code=409, detail="Review the latest version before sharing"
        )
    _validated_medication_reviews(session, context, source, body.medication_reviews)
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
    title, content = decrypt_version(source)
    if entry.origin == "human":
        # A staff-authored note is itself an immutable primary source. Bind the
        # clinician approval to the full requested version instead of requiring
        # staff to manufacture a separate highlight first.
        pointers = [
            _whole_version_pointer(
                session,
                clinic_id=context.clinic_id,
                version=source,
                content=content,
            )
        ]
    else:
        pointers = list(
            session.exec(
                select(ProvenancePointer).where(
                    ProvenancePointer.clinic_id == context.clinic_id,
                    ProvenancePointer.entry_version_id == source.id,
                    ProvenancePointer.anchor_state == "resolved",
                    col(ProvenancePointer.review_required).is_(False),
                )
            ).all()
        )
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
                Highlight.clinic_id == context.clinic_id,
                Highlight.source_entry_version_id == source.id,
            )
        ).all()
        confidence_qualifications = [
            requalify_assessment_confidence(session, item) for item in assessments
        ]
        if not assessments or any(
            item.abstained
            or item.support_state != "supported"
            or qualification.band not in {"high", "medium"}
            or not qualification.qualified
            for item, qualification in zip(
                assessments, confidence_qualifications, strict=True
            )
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "DECISION_ASSESSMENT_NOT_PUBLISHABLE"},
            )
        # Containment in one direction is not enough: it would let a pointer
        # whose highlight was never assessed publish alongside assessed ones,
        # and the publication item below would then record it as human
        # asserted. Every published claim must be an assessed claim, and every
        # model-derived highlight on this version must be published.
        pointer_highlight_ids = {pointer.highlight_id for pointer in pointers}
        assessed_highlight_ids = {item.highlight_id for item in assessments}
        coverage = ai_assessment_coverage(
            session,
            clinic_id=context.clinic_id,
            patient_id=entry.patient_id,
            source_entry_version_id=source.id,
        )
        if (
            None in pointer_highlight_ids
            or pointer_highlight_ids != assessed_highlight_ids
            or not coverage.complete
            or coverage.expected_highlight_ids != assessed_highlight_ids
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "CLAIM_LEVEL_PROVENANCE_REQUIRED"},
            )
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
        medication_review_verified=True,
        commit=False,
    )
    publication = session.exec(
        select(PatientPublication).where(
            PatientPublication.clinic_id == context.clinic_id,
            PatientPublication.patient_id == entry.patient_id,
            PatientPublication.entry_id == entry.id,
            PatientPublication.entry_version_id == updated.version_id,
            col(PatientPublication.withdrawn_at).is_(None),
        )
    ).first()
    if publication is None:
        raise HTTPException(status_code=500, detail="Publication record missing")
    published_version = session.get(EntryVersion, updated.version_id)
    if published_version is None or published_version.clinic_id != context.clinic_id:
        raise HTTPException(status_code=500, detail="Publication version missing")
    publication_medication_reviews = _bind_medication_reviews_to_published_version(
        session,
        context,
        source,
        published_version,
        body.medication_reviews,
    )
    publication.medication_review_complete = bool(publication_medication_reviews)
    publication.medication_review_json = publication_medication_reviews
    publication.medication_reviewed_by_membership_id = (
        context.membership.id if publication_medication_reviews else None
    )
    publication.medication_reviewed_at = (
        get_datetime_utc() if publication_medication_reviews else None
    )
    publication.correction_reason_code = body.correction_reason_code
    session.add(publication)
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
        if assessment is None and entry.origin in {"ai", "system"}:
            # The equality gate above makes this unreachable. Recording a
            # model-derived claim as human asserted would be a false provenance
            # statement in the patient-visible record, so fail instead.
            raise HTTPException(
                status_code=409,
                detail={"code": "CLAIM_LEVEL_PROVENANCE_REQUIRED"},
            )
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
        request.publication_id = publication.id
        request.reviewed_by_membership_id = context.membership.id
        request.reviewed_at = get_datetime_utc()
        session.add(request)
    if sharing_request is not None and sharing_request not in pending_requests:
        sharing_request.status = "approved"
        sharing_request.publication_id = publication.id
        sharing_request.reviewed_by_membership_id = context.membership.id
        sharing_request.reviewed_at = get_datetime_utc()
        session.add(sharing_request)
    if commit:
        session.commit()
        session.refresh(publication)
    else:
        session.flush()
    return _publication_public(session, publication)


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
    return _publish_for_patient(entry_id, body, session, context, commit=True)


def _add_patient_portal_event(
    session: Session,
    context: RequestContext,
    *,
    patient_id: uuid.UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, object],
) -> PatientPortalEvent:
    event_id = uuid.uuid4()
    event = PatientPortalEvent(
        id=event_id,
        clinic_id=context.clinic_id,
        patient_id=patient_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload_ciphertext=field_codec.encrypt_json(
            context.clinic_id,
            "patient_portal_event.payload",
            event_id,
            payload,
        ),
    )
    session.add(event)
    session.flush()
    return event


def _correction_request_sha256(
    publication_id: uuid.UUID, body: PatientPublicationCorrectionCreate
) -> str:
    """Hash the canonical target plus validated correction request body."""

    canonical_request = {
        "publication_id": str(publication_id),
        "body": body.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(
            canonical_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@router.post(
    "/patient-publications/{publication_id}/correct",
    response_model=PatientPublicationPublic,
    status_code=201,
)
def correct_patient_publication(
    publication_id: uuid.UUID,
    body: PatientPublicationCorrectionCreate,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str = Header(
        min_length=8, max_length=200, alias="Idempotency-Key"
    ),
) -> PatientPublicationPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician role required")
    withdrawn = session.exec(
        select(PatientPublication)
        .where(
            PatientPublication.id == publication_id,
            PatientPublication.clinic_id == context.clinic_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if withdrawn is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    request_sha256 = _correction_request_sha256(withdrawn.id, body)
    idempotency_key_sha256 = hashlib.sha256(idempotency_key.encode()).hexdigest()
    idempotency_replay = session.exec(
        select(PatientPublication).where(
            PatientPublication.clinic_id == context.clinic_id,
            PatientPublication.correction_idempotency_key_sha256
            == idempotency_key_sha256,
        )
    ).first()
    if idempotency_replay is not None:
        if (
            idempotency_replay.supersedes_publication_id != withdrawn.id
            or idempotency_replay.correction_request_sha256 != request_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PUBLICATION_CORRECTION_IDEMPOTENCY_CONFLICT",
                    "replacement_publication_id": str(idempotency_replay.id),
                },
            )
        return _publication_public(session, idempotency_replay)
    replay = session.exec(
        select(PatientPublication).where(
            PatientPublication.clinic_id == context.clinic_id,
            PatientPublication.supersedes_publication_id == withdrawn.id,
        )
    ).first()
    if replay is not None:
        if (
            replay.correction_request_sha256 != request_sha256
            or replay.correction_idempotency_key_sha256 != idempotency_key_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PUBLICATION_CORRECTION_ALREADY_REPLACED",
                    "replacement_publication_id": str(replay.id),
                },
            )
        return _publication_public(session, replay)
    if withdrawn.withdrawn_at is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "PUBLICATION_ALREADY_WITHDRAWN"},
        )
    replacement_source = session.exec(
        select(EntryVersion).where(
            EntryVersion.id == body.replacement_entry_version_id,
            EntryVersion.clinic_id == context.clinic_id,
            EntryVersion.entry_id == withdrawn.entry_id,
        )
    ).first()
    if replacement_source is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "REPLACEMENT_VERSION_MISMATCH"},
        )
    replacement_public = _publish_for_patient(
        withdrawn.entry_id,
        PatientPublicationCreate(
            entry_version_id=replacement_source.id,
            medication_reviews=body.medication_reviews,
            correction_reason_code="patient_summary_correction",
        ),
        session,
        context,
        commit=False,
    )
    replacement = session.get(PatientPublication, replacement_public.id)
    if replacement is None or replacement.supersedes_publication_id != withdrawn.id:
        raise HTTPException(status_code=500, detail="Correction publication missing")
    replacement.correction_idempotency_key_sha256 = idempotency_key_sha256
    replacement.correction_request_sha256 = request_sha256
    session.add(replacement)
    withdrawn.withdrawn_by_membership_id = context.membership.id
    withdrawn.correction_reason_code = "patient_summary_corrected"
    session.add(withdrawn)
    _add_patient_portal_event(
        session,
        context,
        patient_id=withdrawn.patient_id,
        event_type="patient_publication.corrected",
        aggregate_type="patient_publication",
        aggregate_id=withdrawn.id,
        payload={
            "withdrawn_publication_id": str(withdrawn.id),
            "replacement_publication_id": str(replacement.id),
        },
    )
    notification: NotificationOutbox | None = None
    try:
        # Keep the outbox intent transactional whenever queue construction is
        # healthy, but isolate adapter/configuration faults so the clinical
        # correction and its outreach work item remain durable and visible.
        with session.begin_nested():
            notification, _ = queue_notification(
                session,
                clinic_id=context.clinic_id,
                purpose="correction",
                channel="portal",
                destination=f"portal:{withdrawn.patient_id}",
                template_key="patient_publication_correction",
                payload={
                    "withdrawn_publication_id": str(withdrawn.id),
                    "replacement_publication_id": str(replacement.id),
                },
                idempotency_key=(
                    f"publication-correction:{withdrawn.id}:{idempotency_key_sha256}"
                ),
                patient_id=withdrawn.patient_id,
                publication_id=replacement.id,
                created_by_membership_id=context.membership.id,
            )
    except Exception:
        notification = None
    session.add(
        PublicationCorrectionOutreach(
            clinic_id=context.clinic_id,
            patient_id=withdrawn.patient_id,
            withdrawn_publication_id=withdrawn.id,
            replacement_publication_id=replacement.id,
            notification_id=notification.id if notification is not None else None,
            status="pending",
            due_at=get_datetime_utc() + timedelta(hours=24),
        )
    )
    emit_change(
        session,
        context,
        action="patient_publication.corrected",
        resource_type="patient_publication",
        resource_id=withdrawn.id,
        metadata={"replacement_publication_id": str(replacement.id)},
        reason_code="patient_summary_correction",
    )
    rebuild_glance(session, context, withdrawn.patient_id)
    # The correction, invalidation event, queued outbox intent, and outreach
    # are one clinical transaction. Provider submission starts only after that
    # durable boundary, so delivery failure can never roll back the correction.
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        collision = session.exec(
            select(PatientPublication).where(
                PatientPublication.clinic_id == context.clinic_id,
                PatientPublication.correction_idempotency_key_sha256
                == idempotency_key_sha256,
            )
        ).first()
        if collision is None:
            raise
        if (
            collision.supersedes_publication_id == withdrawn.id
            and collision.correction_request_sha256 == request_sha256
        ):
            return _publication_public(session, collision)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PUBLICATION_CORRECTION_IDEMPOTENCY_CONFLICT",
                "replacement_publication_id": str(collision.id),
            },
        ) from None
    if notification is not None:
        persisted_notification = session.get(NotificationOutbox, notification.id)
        if persisted_notification is not None:
            try:
                dispatch_notification(session, persisted_notification)
                session.commit()
            except Exception:
                # The correction and queued intent crossed their durable
                # boundary above. Persist an explicit retryable delivery state
                # so the 201 response cannot look like an unqualified success.
                session.rollback()
                failed_notification = session.exec(
                    select(NotificationOutbox)
                    .where(
                        NotificationOutbox.clinic_id == context.clinic_id,
                        NotificationOutbox.id == notification.id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).first()
                if failed_notification is not None:
                    failed_at = get_datetime_utc()
                    failed_notification.state = "failed"
                    failed_notification.failed_at = failed_at
                    failed_notification.available_at = failed_at + timedelta(seconds=30)
                    failed_notification.updated_at = failed_at
                    session.add(failed_notification)
                    session.commit()
    session.refresh(replacement)
    return _publication_public(session, replacement)


@router.post(
    "/patient-sharing-requests/{request_id}/approve",
    response_model=PatientPublicationPublic,
    status_code=201,
)
def approve_patient_sharing_request(
    request_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    body: PatientSharingApprovalCreate | None = None,
) -> PatientPublicationPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician approval required")
    request = session.exec(
        select(PatientSharingRequest)
        .where(
            PatientSharingRequest.id == request_id,
            PatientSharingRequest.clinic_id == context.clinic_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if request is None:
        raise HTTPException(status_code=404, detail="Sharing request not found")
    return publish_for_patient(
        request.entry_id,
        PatientPublicationCreate(
            entry_version_id=request.entry_version_id,
            sharing_request_id=request.id,
            medication_reviews=body.medication_reviews if body is not None else [],
        ),
        session,
        context,
    )


@router.post(
    "/patient-publications/{publication_id}/acknowledgements",
    response_model=PatientPublicationAcknowledgementPublic,
    status_code=201,
)
def acknowledge_patient_publication(
    publication_id: uuid.UUID,
    body: PatientPublicationAcknowledgementCreate,
    session: SessionDep,
    context: CurrentContext,
) -> PatientPublicationAcknowledgementPublic:
    if context.role != "patient":
        raise HTTPException(status_code=403, detail="Patient access required")
    publication = session.exec(
        select(PatientPublication).where(
            PatientPublication.id == publication_id,
            PatientPublication.clinic_id == context.clinic_id,
        )
    ).first()
    if publication is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    get_patient(session, context, publication.patient_id)
    existing = session.exec(
        select(PatientPublicationAcknowledgement).where(
            PatientPublicationAcknowledgement.clinic_id == context.clinic_id,
            PatientPublicationAcknowledgement.publication_id == publication.id,
            PatientPublicationAcknowledgement.acknowledged_by_user_id
            == context.user_id,
            PatientPublicationAcknowledgement.event_type == body.event_type,
        )
    ).first()
    if existing is not None:
        return PatientPublicationAcknowledgementPublic.model_validate(existing)
    notification: NotificationOutbox | None = None
    if body.notification_id is not None:
        related_publication_ids = {publication.id}
        if publication.supersedes_publication_id is not None:
            related_publication_ids.add(publication.supersedes_publication_id)
        replacement_id = session.exec(
            select(PatientPublication.id).where(
                PatientPublication.clinic_id == context.clinic_id,
                PatientPublication.supersedes_publication_id == publication.id,
            )
        ).first()
        if replacement_id is not None:
            related_publication_ids.add(replacement_id)
        notification = session.exec(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.id == body.notification_id,
                NotificationOutbox.clinic_id == context.clinic_id,
                NotificationOutbox.patient_id == publication.patient_id,
                col(NotificationOutbox.publication_id).in_(related_publication_ids),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if notification is None:
            raise HTTPException(status_code=404, detail="Notification not found")
    acknowledgement = PatientPublicationAcknowledgement(
        clinic_id=context.clinic_id,
        patient_id=publication.patient_id,
        publication_id=publication.id,
        notification_id=notification.id if notification is not None else None,
        acknowledged_by_user_id=context.user_id,
        channel="portal",
        event_type=body.event_type,
    )
    session.add(acknowledgement)
    if body.event_type == "acknowledged":
        completed_at = get_datetime_utc()
        if notification is not None:
            notification.state = "acknowledged"
            notification.acknowledged_at = completed_at
            notification.updated_at = completed_at
            session.add(notification)
        outreach_rows = session.exec(
            select(PublicationCorrectionOutreach).where(
                PublicationCorrectionOutreach.clinic_id == context.clinic_id,
                (
                    PublicationCorrectionOutreach.withdrawn_publication_id
                    == publication.id
                )
                | (
                    PublicationCorrectionOutreach.replacement_publication_id
                    == publication.id
                ),
            )
        ).all()
        for outreach in outreach_rows:
            outreach.status = "acknowledged"
            outreach.completed_at = completed_at
            session.add(outreach)
    _add_patient_portal_event(
        session,
        context,
        patient_id=publication.patient_id,
        event_type=f"patient_publication.{body.event_type}",
        aggregate_type="patient_publication",
        aggregate_id=publication.id,
        payload={"publication_id": str(publication.id)},
    )
    emit_change(
        session,
        context,
        action=f"patient_publication.{body.event_type}",
        resource_type="patient_publication",
        resource_id=publication.id,
        reason_code=f"patient_{body.event_type}",
    )
    session.commit()
    session.refresh(acknowledgement)
    return PatientPublicationAcknowledgementPublic.model_validate(acknowledgement)


@router.get(
    "/patients/{patient_id}/portal-events",
    response_model=list[PatientPortalEventPublic],
)
def list_patient_portal_events(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    since: datetime | None = None,
    limit: int = 100,
) -> list[PatientPortalEventPublic]:
    if context.role not in {"patient", "staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Patient portal access required")
    get_patient(session, context, patient_id)
    statement = select(PatientPortalEvent).where(
        PatientPortalEvent.clinic_id == context.clinic_id,
        PatientPortalEvent.patient_id == patient_id,
    )
    if since is not None:
        statement = statement.where(PatientPortalEvent.created_at > since)
    rows = session.exec(
        statement.order_by(col(PatientPortalEvent.created_at)).limit(
            max(1, min(limit, 500))
        )
    ).all()
    return [PatientPortalEventPublic.model_validate(item) for item in rows]


@router.get("/patients/{patient_id}/portal-events/stream")
def stream_patient_portal_events(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
    since: datetime | None = None,
) -> StreamingResponse:
    """Keep a durable invalidation feed open until cancellation or token expiry."""

    if context.role not in {"patient", "staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Patient portal access required")
    get_patient(session, context, patient_id)

    clinic_id = context.clinic_id
    actor_id = context.user_id
    membership_id = context.membership.id
    role = context.role
    linked_patient_id = context.linked_patient_id
    token_expires_at = context.token_expires_at_epoch
    # The long-lived response must not pin the request-scoped database
    # connection. Every poll below uses its own short RLS-bound session.
    session.rollback()

    async def generate() -> AsyncIterator[str]:
        yield "retry: 1000\n\n"
        cursor_time = since or datetime(1970, 1, 1, tzinfo=UTC)
        cursor_id: uuid.UUID | None = None
        heartbeat_at = time.monotonic()
        while token_expires_at is None or time.time() < token_expires_at:
            session_revoked = False
            rows: Sequence[PatientPortalEvent]
            with Session(engine) as event_session:
                set_rls_clinic(event_session, clinic_id)
                set_rls_actor(
                    event_session,
                    actor_id,
                    role=role,
                    patient_id=linked_patient_id,
                )
                live_user = event_session.get(User, actor_id)
                live_membership = event_session.get(ClinicMembership, membership_id)
                session_revoked = (
                    live_user is None
                    or not live_user.is_active
                    or live_membership is None
                    or not live_membership.is_active
                    or live_membership.user_id != actor_id
                    or live_membership.clinic_id != clinic_id
                    or live_membership.role != role
                )
                if not session_revoked and role == "patient":
                    live_link = event_session.exec(
                        select(PatientUserLink).where(
                            PatientUserLink.clinic_id == clinic_id,
                            PatientUserLink.user_id == actor_id,
                            PatientUserLink.patient_id == linked_patient_id,
                        )
                    ).first()
                    session_revoked = live_link is None
                if session_revoked:
                    rows = []
                else:
                    statement = select(PatientPortalEvent).where(
                        PatientPortalEvent.clinic_id == clinic_id,
                        PatientPortalEvent.patient_id == patient_id,
                    )
                    if cursor_id is None:
                        statement = statement.where(
                            PatientPortalEvent.created_at > cursor_time
                        )
                    else:
                        statement = statement.where(
                            (PatientPortalEvent.created_at > cursor_time)
                            | (
                                (PatientPortalEvent.created_at == cursor_time)
                                & (PatientPortalEvent.id > cursor_id)
                            )
                        )
                    rows = event_session.exec(
                        statement.order_by(
                            col(PatientPortalEvent.created_at),
                            col(PatientPortalEvent.id),
                        ).limit(100)
                    ).all()
            if session_revoked:
                yield (
                    "event: session.revoked\n"
                    'data: {"reason_code":"SESSION_REVOKED"}\n\n'
                )
                return
            if rows:
                for row in rows:
                    event = PatientPortalEventPublic.model_validate(row)
                    yield (
                        f"id: {event.id}\n"
                        f"event: {event.event_type}\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
                    cursor_time = row.created_at
                    cursor_id = row.id
                heartbeat_at = time.monotonic()
                # Drain a burst without an avoidable one-second delay.
                if len(rows) == 100:
                    await asyncio.sleep(0)
                    continue
            elif time.monotonic() - heartbeat_at >= 15.0:
                yield ": heartbeat\n\n"
                heartbeat_at = time.monotonic()
            await asyncio.sleep(1.0)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/patients/{patient_id}/publication-correction-outreach",
    response_model=list[PublicationCorrectionOutreachPublic],
)
def list_publication_correction_outreach(
    patient_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> list[PublicationCorrectionOutreachPublic]:
    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Clinical team role required")
    get_patient(session, context, patient_id)
    rows = session.exec(
        select(PublicationCorrectionOutreach)
        .where(
            PublicationCorrectionOutreach.clinic_id == context.clinic_id,
            PublicationCorrectionOutreach.patient_id == patient_id,
        )
        .order_by(col(PublicationCorrectionOutreach.created_at).desc())
    ).all()
    return [PublicationCorrectionOutreachPublic.model_validate(item) for item in rows]


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
        select(PatientPublication)
        .where(
            PatientPublication.id == publication_id,
            PatientPublication.clinic_id == context.clinic_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if publication is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    if publication.withdrawn_at is None:
        publication.withdrawn_at = get_datetime_utc()
        publication.withdrawn_by_membership_id = context.membership.id
        session.add(publication)
        source = session.get(EntryVersion, publication.entry_version_id)
        entry = session.get(Entry, source.entry_id) if source is not None else None
        if (
            source is None
            or entry is None
            or source.clinic_id != context.clinic_id
            or entry.clinic_id != context.clinic_id
        ):
            raise HTTPException(status_code=500, detail="Publication source missing")
        approved_requests = session.exec(
            select(PatientSharingRequest).where(
                PatientSharingRequest.clinic_id == context.clinic_id,
                PatientSharingRequest.patient_id == publication.patient_id,
                PatientSharingRequest.entry_id == publication.entry_id,
                PatientSharingRequest.publication_id == publication.id,
                PatientSharingRequest.status == "approved",
            )
        ).all()
        for request in approved_requests:
            request.status = "withdrawn"
            request.reviewed_by_membership_id = context.membership.id
            request.reviewed_at = get_datetime_utc()
            session.add(request)
        emit_change(
            session,
            context,
            action="patient_publication.withdrawn",
            resource_type="patient_publication",
            resource_id=publication.id,
            metadata={"entry_id": str(entry.id)},
        )
        other_active = session.exec(
            select(PatientPublication).where(
                PatientPublication.clinic_id == context.clinic_id,
                PatientPublication.patient_id == publication.patient_id,
                PatientPublication.entry_id == publication.entry_id,
                PatientPublication.id != publication.id,
                col(PatientPublication.withdrawn_at).is_(None),
            )
        ).first()
        if (
            other_active is None
            and entry.patient_facing
            and entry.current_version_id is not None
        ):
            current = session.get(EntryVersion, entry.current_version_id)
            if current is None:
                raise HTTPException(status_code=500, detail="Entry version missing")
            title, content = decrypt_version(current)
            patch_entry(
                session,
                context,
                entry.id,
                if_match=str(current.id),
                title=title,
                content=content,
                patient_facing=False,
                action="entry.patient_sharing_withdrawn",
                withdrawn_patient_sharing=True,
                commit=False,
            )
        else:
            rebuild_glance(session, context, publication.patient_id)
        _add_patient_portal_event(
            session,
            context,
            patient_id=publication.patient_id,
            event_type="patient_publication.withdrawn",
            aggregate_type="patient_publication",
            aggregate_id=publication.id,
            payload={"withdrawn_publication_id": str(publication.id)},
        )
        notification, _ = queue_notification(
            session,
            clinic_id=context.clinic_id,
            purpose="correction",
            channel="portal",
            destination=f"portal:{publication.patient_id}",
            template_key="patient_publication_withdrawal",
            payload={"withdrawn_publication_id": str(publication.id)},
            idempotency_key=f"publication-withdrawal:{publication.id}",
            patient_id=publication.patient_id,
            publication_id=publication.id,
            created_by_membership_id=context.membership.id,
        )
        existing_outreach = session.exec(
            select(PublicationCorrectionOutreach).where(
                PublicationCorrectionOutreach.clinic_id == context.clinic_id,
                PublicationCorrectionOutreach.withdrawn_publication_id
                == publication.id,
                col(PublicationCorrectionOutreach.replacement_publication_id).is_(None),
            )
        ).first()
        if existing_outreach is None:
            session.add(
                PublicationCorrectionOutreach(
                    clinic_id=context.clinic_id,
                    patient_id=publication.patient_id,
                    withdrawn_publication_id=publication.id,
                    notification_id=notification.id,
                    status="pending",
                    due_at=get_datetime_utc() + timedelta(hours=24),
                )
            )
        # Persist clinical withdrawal, portal invalidation, outbox, and
        # outreach before invoking even a deterministic provider adapter.
        session.commit()
        persisted_notification = session.get(NotificationOutbox, notification.id)
        if persisted_notification is None:
            raise HTTPException(status_code=409, detail="Notification unavailable")
        dispatch_notification(session, persisted_notification)
        session.commit()
        session.refresh(publication)
    return _publication_public(session, publication)
