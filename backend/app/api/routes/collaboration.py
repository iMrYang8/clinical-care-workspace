import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from fastapi import APIRouter, Header, HTTPException, Response
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
    DomainEvent,
    EditorPresenceHeartbeatCreate,
    EditorPresencePublic,
    Highlight,
    ProvenancePointer,
    get_datetime_utc,
)
from app.services.importance import record_feedback
from app.services.nightingale import (
    VersionConflictError,
    decrypt_version,
    emit_change,
    get_scoped_entry,
    get_scoped_version,
    normalize_etag,
    rebuild_glance,
    validate_anchor,
)

router = APIRouter(tags=["collaboration"])

EDITOR_PRESENCE_TTL_SECONDS = 45
COMMENT_MUTATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Comment mutation applied or already in the requested state.",
        "headers": {
            "ETag": {
                "description": "Quoted current comment revision for the next mutation.",
                "schema": {"type": "string"},
            }
        },
    },
    409: {"description": "The supplied comment revision is stale."},
    428: {"description": "If-Match is required."},
}


def _require_collaborator(context: RequestContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(
            status_code=403, detail="Internal collaboration role required"
        )


def _require_collaboration_reader(context: RequestContext) -> None:
    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(
            status_code=403, detail="Internal collaboration access required"
        )


def _comment_public(session: Session, comment: Comment) -> CommentPublic:
    mentioned_user_ids = list(
        session.exec(
            select(CommentMention.mentioned_user_id)
            .where(
                CommentMention.clinic_id == comment.clinic_id,
                CommentMention.comment_id == comment.id,
            )
            .order_by(col(CommentMention.created_at))
        ).all()
    )
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
        revision=comment.revision,
        mentioned_user_ids=mentioned_user_ids,
        resolved_at=comment.resolved_at,
        created_at=comment.created_at,
    )


def _get_comment(
    session: Session,
    context: RequestContext,
    comment_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Comment:
    _require_collaborator(context)
    statement = select(Comment).where(
        Comment.id == comment_id, Comment.clinic_id == context.clinic_id
    )
    if lock:
        statement = statement.with_for_update()
    comment = session.exec(statement).first()
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
    if membership.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=422, detail="Assignee must be clinical staff")


def _require_comment_revision(comment: Comment, if_match: str | None) -> None:
    """Enforce the row-locked comment revision as an HTTP precondition."""

    current_etag = f'"{comment.revision}"'
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail="If-Match is required",
            headers={"ETag": current_etag},
        )
    if normalize_etag(if_match) != str(comment.revision):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "COMMENT_VERSION_CONFLICT",
                "latest_revision": comment.revision,
            },
            headers={"ETag": current_etag},
        )


def _set_comment_etag(response: Response, comment: Comment) -> None:
    response.headers["ETag"] = f'"{comment.revision}"'


def _create_comment(
    session: Session,
    context: RequestContext,
    entry_id: uuid.UUID,
    body: CommentCreate,
    *,
    parent_override: uuid.UUID | None = None,
    if_match: str,
) -> CommentPublic:
    _require_collaborator(context)
    entry = get_scoped_entry(session, context, entry_id, lock=True)
    if (
        entry.current_version_id is None
        or normalize_etag(if_match) != str(entry.current_version_id)
        or body.entry_version_id != entry.current_version_id
    ):
        if entry.current_version_id is None:
            raise HTTPException(status_code=500, detail="Entry has no current version")
        raise VersionConflictError(entry.current_version_id)
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
        if membership.role not in {"staff", "clinician"}:
            raise HTTPException(
                status_code=422, detail="Mentioned user must be clinical staff"
            )
        session.add(
            CommentMention(
                clinic_id=context.clinic_id,
                comment_id=comment.id,
                mentioned_user_id=user_id,
            )
        )
    affected_patients: set[uuid.UUID] = set()
    if anchor_state == "resolved":
        related_highlights = session.exec(
            select(Highlight).where(
                Highlight.clinic_id == context.clinic_id,
                Highlight.source_entry_version_id == version.id,
            )
        ).all()
        for highlight in related_highlights:
            _, affected = record_feedback(
                session,
                context,
                highlight,
                signal="comment",
                idempotency_key=f"comment:{comment.id}:highlight:{highlight.id}",
            )
            affected_patients.update(affected)
    emit_change(
        session,
        context,
        action="comment.created",
        resource_type="comment",
        resource_id=comment.id,
        metadata={"entry_id": str(entry.id), "anchor_state": anchor_state},
    )
    for patient_id in affected_patients:
        rebuild_glance(session, context, patient_id)
    session.commit()
    session.refresh(comment)
    return _comment_public(session, comment)


@router.get("/entries/{entry_id}/comments", response_model=list[CommentPublic])
def list_comments(
    entry_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> list[CommentPublic]:
    _require_collaboration_reader(context)
    get_scoped_entry(session, context, entry_id)
    comments = session.exec(
        select(Comment)
        .where(Comment.clinic_id == context.clinic_id, Comment.entry_id == entry_id)
        .order_by(col(Comment.created_at))
    ).all()
    return [_comment_public(session, comment) for comment in comments]


@router.post(
    "/entries/{entry_id}/presence",
    response_model=EditorPresencePublic,
)
def heartbeat_editor_presence(
    entry_id: uuid.UUID,
    body: EditorPresenceHeartbeatCreate,
    session: SessionDep,
    context: CurrentContext,
) -> EditorPresencePublic:
    """Publish a short-lived, content-free editing signal on the clinic SSE bus."""

    _require_collaborator(context)
    entry = get_scoped_entry(session, context, entry_id)
    version = get_scoped_version(session, context, entry, body.entry_version_id)
    actor_role = cast(Literal["staff", "clinician"], context.role)
    presence = EditorPresencePublic(
        clinic_id=context.clinic_id,
        patient_id=entry.patient_id,
        entry_id=entry.id,
        entry_version_id=version.id,
        actor_id=context.user_id,
        actor_role=actor_role,
        actor_display_name=(
            context.user.full_name
            or ("Clinician" if actor_role == "clinician" else "Care staff")
        ),
        expires_at=get_datetime_utc() + timedelta(seconds=EDITOR_PRESENCE_TTL_SECONDS),
    )
    # Presence is intentionally a transient domain event rather than an audit
    # event.  It contains no draft, cursor, selection, or free-text metadata;
    # consumers must discard it at ``expires_at``.
    session.add(
        DomainEvent(
            clinic_id=context.clinic_id,
            event_type="editor_presence",
            aggregate_type="entry",
            aggregate_id=entry.id,
            actor_id=context.user_id,
            payload_json=presence.model_dump(mode="json"),
        )
    )
    session.commit()
    return presence


@router.post(
    "/entries/{entry_id}/comments", response_model=CommentPublic, status_code=201
)
def create_comment(
    entry_id: uuid.UUID,
    body: CommentCreate,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CommentPublic:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    return _create_comment(session, context, entry_id, body, if_match=if_match)


@router.post(
    "/comments/{comment_id}/replies", response_model=CommentPublic, status_code=201
)
def reply(
    comment_id: uuid.UUID,
    body: CommentCreate,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CommentPublic:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    parent = _get_comment(session, context, comment_id)
    return _create_comment(
        session,
        context,
        parent.entry_id,
        body,
        parent_override=parent.id,
        if_match=if_match,
    )


@router.post(
    "/comments/{comment_id}/resolve",
    response_model=CommentPublic,
    responses=COMMENT_MUTATION_RESPONSES,
)
@router.patch(
    "/comments/{comment_id}/resolve",
    response_model=CommentPublic,
    include_in_schema=False,
)
def resolve(
    comment_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CommentPublic:
    comment = _get_comment(session, context, comment_id, lock=True)
    _require_comment_revision(comment, if_match)
    if comment.resolved_at is None:
        comment.resolved_at = datetime.now(UTC)
        comment.revision += 1
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
    _set_comment_etag(response, comment)
    return _comment_public(session, comment)


@router.post(
    "/comments/{comment_id}/unresolve",
    response_model=CommentPublic,
    responses=COMMENT_MUTATION_RESPONSES,
)
@router.patch(
    "/comments/{comment_id}/unresolve",
    response_model=CommentPublic,
    include_in_schema=False,
)
def unresolve(
    comment_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CommentPublic:
    comment = _get_comment(session, context, comment_id, lock=True)
    _require_comment_revision(comment, if_match)
    if comment.resolved_at is not None:
        comment.resolved_at = None
        comment.revision += 1
        session.add(comment)
        emit_change(
            session,
            context,
            action="comment.unresolved",
            resource_type="comment",
            resource_id=comment.id,
            metadata={"entry_id": str(comment.entry_id)},
        )
    session.commit()
    session.refresh(comment)
    _set_comment_etag(response, comment)
    return _comment_public(session, comment)


@router.patch(
    "/comments/{comment_id}/assignment",
    response_model=CommentPublic,
    responses=COMMENT_MUTATION_RESPONSES,
)
def assign(
    comment_id: uuid.UUID,
    body: AssignmentUpdate,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CommentPublic:
    comment = _get_comment(session, context, comment_id, lock=True)
    _require_comment_revision(comment, if_match)
    _validate_membership(session, context, body.assigned_membership_id)
    comment.assigned_membership_id = body.assigned_membership_id
    comment.revision += 1
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
    _set_comment_etag(response, comment)
    return _comment_public(session, comment)
