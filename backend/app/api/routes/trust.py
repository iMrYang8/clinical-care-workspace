import hashlib
import uuid

from fastapi import APIRouter, Header, HTTPException
from sqlmodel import Session, col, select

from app.api.deps import CurrentContext, RequestContext, SessionDep
from app.core.field_crypto import field_codec
from app.models import (
    Entry,
    EntryVersion,
    Highlight,
    HighlightCreate,
    HighlightPublic,
    ImportanceFeedbackCreate,
    ProvenancePointer,
    ProvenanceResolved,
)
from app.services.importance import (
    record_feedback,
    refresh_highlight_score,
    sanitize_feature_keys,
)
from app.services.nightingale import (
    decrypt_version,
    emit_change,
    get_scoped_entry,
    get_scoped_version,
    rebuild_glance,
    resolve_pointer,
    validate_anchor,
)

router = APIRouter(tags=["trust"])


def _require_reviewer(context: RequestContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical review role required")


def _get_highlight(
    session: Session, context: RequestContext, highlight_id: uuid.UUID
) -> Highlight:
    _require_reviewer(context)
    highlight = session.exec(
        select(Highlight).where(
            Highlight.id == highlight_id, Highlight.clinic_id == context.clinic_id
        )
    ).first()
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
    if body.critical and "risk:critical" not in feature_keys:
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
        critical=body.critical,
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
    highlight = _get_highlight(session, context, highlight_id)
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
        changed = changed or not highlight.clinician_confirmed or highlight.unresolved
        highlight.clinician_confirmed = True
        highlight.unresolved = False
    if not changed:
        return _highlight_public(session, context, highlight)
    session.add(highlight)
    _, affected_patients = record_feedback(
        session,
        context,
        highlight,
        signal=action,
        idempotency_key=idempotency_key or f"{action}:{highlight.id}",
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
    highlight = _get_highlight(session, context, highlight_id)
    if highlight.status == "dismissed":
        return _highlight_public(session, context, highlight)
    highlight.status = "dismissed"
    highlight.pinned = False
    session.add(highlight)
    _, affected_patients = record_feedback(
        session,
        context,
        highlight,
        signal=body.signal,
        idempotency_key=idempotency_key,
    )
    emit_change(
        session,
        context,
        action=f"highlight.feedback.{body.signal}",
        resource_type="highlight",
        resource_id=highlight.id,
    )
    for patient_id in affected_patients | {highlight.patient_id}:
        rebuild_glance(session, context, patient_id)
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


@router.get("/provenance/{pointer_id}/resolve", response_model=ProvenanceResolved)
def provenance_resolve(
    pointer_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> ProvenanceResolved:
    if context.role in {"admin", "worker"}:
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
