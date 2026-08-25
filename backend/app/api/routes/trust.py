import hashlib
import uuid

from fastapi import APIRouter, HTTPException
from sqlmodel import Session, select

from app.api.deps import CurrentContext, RequestContext, SessionDep
from app.core.field_crypto import field_codec
from app.models import (
    Entry,
    EntryVersion,
    Highlight,
    HighlightCreate,
    HighlightPublic,
    ProvenancePointer,
    ProvenanceResolved,
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
        created_by_id=context.user_id,
    )
    session.add(highlight)
    session.flush()
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
    emit_change(
        session,
        context,
        action="highlight.created",
        resource_type="highlight",
        resource_id=highlight.id,
        metadata={"anchor_state": anchor_state, "entry_version_id": str(version.id)},
    )
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


def _transition(
    session: Session,
    context: RequestContext,
    highlight_id: uuid.UUID,
    action: str,
) -> HighlightPublic:
    highlight = _get_highlight(session, context, highlight_id)
    if action == "accept":
        highlight.status = "accepted"
    elif action == "reject":
        highlight.status = "rejected"
        highlight.pinned = False
    elif action == "pin":
        highlight.pinned = True
    session.add(highlight)
    emit_change(
        session,
        context,
        action=f"highlight.{action}",
        resource_type="highlight",
        resource_id=highlight.id,
    )
    rebuild_glance(session, context, highlight.patient_id)
    session.commit()
    session.refresh(highlight)
    return _highlight_public(session, context, highlight)


@router.post("/highlights/{highlight_id}/accept", response_model=HighlightPublic)
def accept(
    highlight_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> HighlightPublic:
    return _transition(session, context, highlight_id, "accept")


@router.post("/highlights/{highlight_id}/reject", response_model=HighlightPublic)
def reject(
    highlight_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> HighlightPublic:
    return _transition(session, context, highlight_id, "reject")


@router.post("/highlights/{highlight_id}/pin", response_model=HighlightPublic)
def pin(
    highlight_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> HighlightPublic:
    return _transition(session, context, highlight_id, "pin")


@router.get("/provenance/{pointer_id}/resolve", response_model=ProvenanceResolved)
def provenance_resolve(
    pointer_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> ProvenanceResolved:
    pointer = session.exec(
        select(ProvenancePointer).where(
            ProvenancePointer.id == pointer_id,
            ProvenancePointer.clinic_id == context.clinic_id,
        )
    ).first()
    if pointer is None:
        raise HTTPException(status_code=404, detail="Provenance not found")
    version = session.exec(
        select(EntryVersion).where(
            EntryVersion.id == pointer.entry_version_id,
            EntryVersion.clinic_id == context.clinic_id,
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Provenance source not found")
    entry = session.exec(
        select(Entry).where(
            Entry.id == version.entry_id, Entry.clinic_id == context.clinic_id
        )
    ).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Provenance source not found")
    get_scoped_entry(session, context, entry.id)
    resolved = resolve_pointer(pointer, version)
    pointer.anchor_state = resolved["state"]
    pointer.review_required = resolved["review_required"]
    session.add(pointer)
    session.commit()
    return ProvenanceResolved.model_validate(resolved)
