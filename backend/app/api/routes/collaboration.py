import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlmodel import Session, col, select

from app.api.deps import CurrentContext, RequestContext, SessionDep
from app.core.field_crypto import field_codec
from app.models import (
    AssignmentUpdate,
    ClinicMembership,
    Comment,
    CommentCreate,
    CommentMention,
    CommentPublic,
    ProvenancePointer,
)
from app.services.nightingale import (
    decrypt_version,
    emit_change,
    get_scoped_entry,
    get_scoped_version,
    validate_anchor,
)

router = APIRouter(tags=["collaboration"])


def _require_collaborator(context: RequestContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(
            status_code=403, detail="Internal collaboration role required"
        )


def _comment_public(comment: Comment) -> CommentPublic:
    return CommentPublic(
        id=comment.id,
        entry_id=comment.entry_id,
        entry_version_id=comment.entry_version_id,
        parent_id=comment.parent_id,
        author_id=comment.author_id,
        body=field_codec.decrypt_text(
            comment.clinic_id, "comment.body", comment.id, comment.body_ciphertext
        ),
        anchor_state=comment.anchor_state,
        review_required=comment.review_required,
        assigned_membership_id=comment.assigned_membership_id,
        resolved_at=comment.resolved_at,
        created_at=comment.created_at,
    )


def _get_comment(
    session: Session, context: RequestContext, comment_id: uuid.UUID
) -> Comment:
    _require_collaborator(context)
    comment = session.exec(
        select(Comment).where(
            Comment.id == comment_id, Comment.clinic_id == context.clinic_id
        )
    ).first()
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    get_scoped_entry(session, context, comment.entry_id)
    return comment


def _validate_membership(
    session: Session, context: RequestContext, membership_id: uuid.UUID | None
) -> None:
    if membership_id is None:
        return
    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.id == membership_id,
            ClinicMembership.clinic_id == context.clinic_id,
            col(ClinicMembership.is_active).is_(True),
        )
    ).first()
    if membership is None:
        raise HTTPException(status_code=404, detail="Membership not found")


def _create_comment(
    session: Session,
    context: RequestContext,
    entry_id: uuid.UUID,
    body: CommentCreate,
    *,
    parent_override: uuid.UUID | None = None,
) -> CommentPublic:
    _require_collaborator(context)
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
    parent_id = parent_override or body.parent_id
    if parent_id is not None:
        parent = _get_comment(session, context, parent_id)
        if parent.entry_id != entry.id:
            raise HTTPException(status_code=404, detail="Parent comment not found")
    _validate_membership(session, context, body.assigned_membership_id)

    comment_id = uuid.uuid4()
    comment = Comment(
        id=comment_id,
        clinic_id=context.clinic_id,
        entry_id=entry.id,
        entry_version_id=version.id,
        parent_id=parent_id,
        author_id=context.user_id,
        body_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "comment.body", comment_id, body.body
        ),
        start_offset=body.start_offset,
        end_offset=body.end_offset,
        exact_quote_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "comment.exact_quote", comment_id, body.exact_quote
        ),
        prefix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "comment.prefix", comment_id, body.prefix
        ),
        suffix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "comment.suffix", comment_id, body.suffix
        ),
        quote_sha256=quote_hash,
        anchor_state=anchor_state,
        review_required=review_required,
        assigned_membership_id=body.assigned_membership_id,
    )
    session.add(comment)
    session.flush()

    pointer_id = uuid.uuid4()
    session.add(
        ProvenancePointer(
            id=pointer_id,
            clinic_id=context.clinic_id,
            comment_id=comment.id,
            entry_version_id=version.id,
            start_offset=body.start_offset,
            end_offset=body.end_offset,
            exact_quote_ciphertext=field_codec.encrypt_text(
                context.clinic_id,
                "provenance.exact_quote",
                pointer_id,
                body.exact_quote,
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
    )
    for user_id in set(body.mentioned_user_ids):
        membership = session.exec(
            select(ClinicMembership).where(
                ClinicMembership.clinic_id == context.clinic_id,
                ClinicMembership.user_id == user_id,
                col(ClinicMembership.is_active).is_(True),
            )
        ).first()
        if membership is None:
            raise HTTPException(status_code=404, detail="Mentioned user not found")
        session.add(
            CommentMention(
                clinic_id=context.clinic_id,
                comment_id=comment.id,
                mentioned_user_id=user_id,
            )
        )
    emit_change(
        session,
        context,
        action="comment.created",
        resource_type="comment",
        resource_id=comment.id,
        metadata={"entry_id": str(entry.id), "anchor_state": anchor_state},
    )
    session.commit()
    session.refresh(comment)
    return _comment_public(comment)


@router.get("/entries/{entry_id}/comments", response_model=list[CommentPublic])
def list_comments(
    entry_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> list[CommentPublic]:
    _require_collaborator(context)
    get_scoped_entry(session, context, entry_id)
    comments = session.exec(
        select(Comment)
        .where(Comment.clinic_id == context.clinic_id, Comment.entry_id == entry_id)
        .order_by(col(Comment.created_at))
    ).all()
    return [_comment_public(comment) for comment in comments]


@router.post(
    "/entries/{entry_id}/comments", response_model=CommentPublic, status_code=201
)
def create_comment(
    entry_id: uuid.UUID,
    body: CommentCreate,
    session: SessionDep,
    context: CurrentContext,
) -> CommentPublic:
    return _create_comment(session, context, entry_id, body)


@router.post(
    "/comments/{comment_id}/replies", response_model=CommentPublic, status_code=201
)
def reply(
    comment_id: uuid.UUID,
    body: CommentCreate,
    session: SessionDep,
    context: CurrentContext,
) -> CommentPublic:
    parent = _get_comment(session, context, comment_id)
    return _create_comment(
        session, context, parent.entry_id, body, parent_override=parent.id
    )


@router.post("/comments/{comment_id}/resolve", response_model=CommentPublic)
@router.patch(
    "/comments/{comment_id}/resolve",
    response_model=CommentPublic,
    include_in_schema=False,
)
def resolve(
    comment_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> CommentPublic:
    comment = _get_comment(session, context, comment_id)
    comment.resolved_at = datetime.now(UTC)
    session.add(comment)
    emit_change(
        session,
        context,
        action="comment.resolved",
        resource_type="comment",
        resource_id=comment.id,
    )
    session.commit()
    session.refresh(comment)
    return _comment_public(comment)


@router.patch("/comments/{comment_id}/assignment", response_model=CommentPublic)
def assign(
    comment_id: uuid.UUID,
    body: AssignmentUpdate,
    session: SessionDep,
    context: CurrentContext,
) -> CommentPublic:
    comment = _get_comment(session, context, comment_id)
    _validate_membership(session, context, body.assigned_membership_id)
    comment.assigned_membership_id = body.assigned_membership_id
    session.add(comment)
    emit_change(
        session,
        context,
        action="comment.assigned",
        resource_type="comment",
        resource_id=comment.id,
        metadata={
            "assigned_membership_id": str(body.assigned_membership_id)
            if body.assigned_membership_id
            else None
        },
    )
    session.commit()
    session.refresh(comment)
    return _comment_public(comment)
