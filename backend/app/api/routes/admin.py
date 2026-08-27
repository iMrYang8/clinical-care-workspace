import hashlib
import re
import secrets
import uuid
from datetime import timedelta
from typing import cast

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    AuditEvent,
    AuditEventPublic,
    AuditEventsPublic,
    Clinic,
    ClinicAISetting,
    ClinicAISettingPublic,
    ClinicAISettingUpdate,
    ClinicInvitation,
    ClinicMembership,
    MembershipCreate,
    MembershipInvitationPublic,
    MembershipPublic,
    MembershipRole,
    MembershipsPublic,
    Role,
    User,
    get_datetime_utc,
)
from app.services.invitations import deliver_membership_invitation
from app.services.nightingale import emit_change

router = APIRouter(prefix="/admin", tags=["admin"])
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,159}$")


def _require_admin(context: CurrentContext) -> None:
    if context.role != "admin":
        raise HTTPException(status_code=403, detail="Clinic admin role required")


def _ai_setting_public(row: ClinicAISetting | None) -> ClinicAISettingPublic:
    clinic_key_configured = bool(row and row.api_key_ciphertext)
    return ClinicAISettingPublic(
        api_key_configured=clinic_key_configured,
        api_key_last4=row.api_key_last4 if row else None,
        credential_source=(
            "clinic"
            if clinic_key_configured
            else "environment"
            if settings.OPENAI_API_KEY
            else "none"
        ),
        fast_model=row.fast_model if row else "gpt-5-mini",
        careful_model=row.careful_model if row else "gpt-5.1",
        transcribe_model=(row.transcribe_model if row else "gpt-4o-transcribe-diarize"),
        updated_at=row.updated_at if row else None,
    )


@router.get("/ai-settings", response_model=ClinicAISettingPublic)
def ai_settings(session: SessionDep, context: CurrentContext) -> ClinicAISettingPublic:
    _require_admin(context)
    row = session.exec(
        select(ClinicAISetting).where(ClinicAISetting.clinic_id == context.clinic_id)
    ).first()
    return _ai_setting_public(row)


@router.put("/ai-settings", response_model=ClinicAISettingPublic)
def update_ai_settings(
    body: ClinicAISettingUpdate,
    session: SessionDep,
    context: CurrentContext,
) -> ClinicAISettingPublic:
    _require_admin(context)
    for value in (body.fast_model, body.careful_model, body.transcribe_model):
        if not _MODEL_ID.fullmatch(value):
            raise HTTPException(status_code=422, detail="Invalid model identifier")
    if body.api_key and body.clear_api_key:
        raise HTTPException(status_code=422, detail="Choose replace or remove key")

    row = session.exec(
        select(ClinicAISetting)
        .where(ClinicAISetting.clinic_id == context.clinic_id)
        .with_for_update()
    ).first()
    if row is None:
        row = ClinicAISetting(
            clinic_id=context.clinic_id,
            updated_by_membership_id=context.membership.id,
        )
        session.add(row)
        session.flush()

    key_changed = False
    if body.api_key:
        api_key = body.api_key.strip()
        if len(api_key) < 20:
            raise HTTPException(status_code=422, detail="Invalid API key")
        row.api_key_ciphertext = field_codec.encrypt_text(
            context.clinic_id,
            "clinic_ai_setting.api_key",
            row.id,
            api_key,
        )
        row.api_key_last4 = api_key[-4:]
        key_changed = True
    elif body.clear_api_key:
        row.api_key_ciphertext = None
        row.api_key_last4 = None
        key_changed = True

    row.fast_model = body.fast_model
    row.careful_model = body.careful_model
    row.transcribe_model = body.transcribe_model
    row.updated_by_membership_id = context.membership.id
    row.updated_at = get_datetime_utc()
    session.add(row)
    emit_change(
        session,
        context,
        action="clinic.ai_settings.updated",
        resource_type="clinic_ai_setting",
        resource_id=row.id,
        metadata={
            "provider": "openai",
            "fast_model": row.fast_model,
            "careful_model": row.careful_model,
            "transcribe_model": row.transcribe_model,
            "key_changed": key_changed,
        },
    )
    session.commit()
    session.refresh(row)
    return _ai_setting_public(row)


def _public(user: User, membership: ClinicMembership) -> MembershipPublic:
    return MembershipPublic(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=cast(Role, membership.role),
        is_active=membership.is_active,
        created_at=membership.created_at,
    )


def _lock_active_admin(
    session: SessionDep,
    *,
    clinic_id: uuid.UUID,
    membership_id: uuid.UUID,
) -> tuple[ClinicMembership, User]:
    """Revalidate the actor after acquiring the clinic serialization lock."""

    row = session.exec(
        select(ClinicMembership, User)
        .join(User, col(User.id) == ClinicMembership.user_id)
        .where(
            ClinicMembership.clinic_id == clinic_id,
            ClinicMembership.id == membership_id,
        )
        .with_for_update()
    ).first()
    if row is None:
        raise HTTPException(status_code=403, detail="Active clinic admin required")
    membership, user = row
    if not membership.is_active or membership.role != "admin" or not user.is_active:
        raise HTTPException(status_code=403, detail="Active clinic admin required")
    return membership, user


@router.get("/memberships", response_model=MembershipsPublic)
def memberships(session: SessionDep, context: CurrentContext) -> MembershipsPublic:
    _require_admin(context)
    rows = session.exec(
        select(ClinicMembership, User)
        .join(User, col(User.id) == ClinicMembership.user_id)
        .where(ClinicMembership.clinic_id == context.clinic_id)
        .order_by(col(ClinicMembership.created_at), col(ClinicMembership.id))
    ).all()
    data = [_public(user, membership) for membership, user in rows]
    return MembershipsPublic(data=data, count=len(data))


@router.post("/memberships", status_code=201, response_model=MembershipInvitationPublic)
def create_membership(
    body: MembershipCreate, session: SessionDep, context: CurrentContext
) -> MembershipInvitationPublic:
    _require_admin(context)
    normalized_email = str(body.email).strip().lower()
    now = get_datetime_utc()
    clinic = session.exec(
        select(Clinic).where(Clinic.id == context.clinic_id).with_for_update()
    ).first()
    if clinic is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    actor_membership, actor_user = _lock_active_admin(
        session,
        clinic_id=context.clinic_id,
        membership_id=context.membership.id,
    )
    if str(actor_user.email).strip().lower() == normalized_email:
        raise HTTPException(status_code=409, detail="Cannot invite the signed-in admin")

    existing_membership = session.exec(
        select(ClinicMembership)
        .join(User, col(User.id) == ClinicMembership.user_id)
        .where(
            ClinicMembership.clinic_id == context.clinic_id,
            User.email == normalized_email,
        )
        .with_for_update()
    ).first()
    if existing_membership is not None:
        raise HTTPException(status_code=409, detail="Clinic membership already exists")

    pending = session.exec(
        select(ClinicInvitation).where(
            ClinicInvitation.clinic_id == context.clinic_id,
            ClinicInvitation.email == normalized_email,
            col(ClinicInvitation.accepted_at).is_(None),
            col(ClinicInvitation.revoked_at).is_(None),
            ClinicInvitation.expires_at > now,
        )
    ).first()
    if pending is not None:
        raise HTTPException(status_code=409, detail="Active invitation already exists")

    # The admin never looks up or mutates a global User here. That would reveal
    # cross-clinic identity data and let an attacker pre-claim an email. Only
    # the recipient who receives this one-time secret can bind the membership.
    secret = secrets.token_urlsafe(32)
    raw_token = f"{context.clinic_id}.{secret}"
    invitation = ClinicInvitation(
        clinic_id=context.clinic_id,
        email=normalized_email,
        invited_full_name=body.full_name,
        role=body.role,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        created_by_membership_id=actor_membership.id,
        expires_at=now + timedelta(hours=24),
    )
    session.add(invitation)
    session.flush()
    emit_change(
        session,
        context,
        action="membership.invited",
        resource_type="clinic_invitation",
        resource_id=invitation.id,
        metadata={"role": body.role},
    )
    # Commit the invitation and audit record before any external delivery.
    # A recipient must never receive a token for a transaction that can still
    # roll back. Delivery failure revokes this committed token so an admin can
    # safely create and send a replacement.
    session.commit()
    session.refresh(invitation)
    try:
        deliver_membership_invitation(
            recipient=normalized_email,
            token=raw_token,
        )
    except Exception:
        # No token, existing-user fact, or SMTP detail is returned to the admin.
        invitation.revoked_at = get_datetime_utc()
        session.add(invitation)
        emit_change(
            session,
            context,
            action="membership.invitation_delivery_failed",
            resource_type="clinic_invitation",
            resource_id=invitation.id,
            metadata={"role": body.role},
        )
        session.commit()
        raise HTTPException(
            status_code=503,
            detail="Invitation delivery did not complete",
        )
    return MembershipInvitationPublic(
        id=invitation.id,
        email=invitation.email,
        full_name=invitation.invited_full_name,
        role=cast(MembershipRole, invitation.role),
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
    )


@router.post("/memberships/{membership_id}/deactivate", response_model=MembershipPublic)
def deactivate_membership(
    membership_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> MembershipPublic:
    _require_admin(context)
    # Serialize admin removals on a stable clinic row. Without this lock, two
    # admins can concurrently observe count=2 and deactivate each other.
    clinic = session.exec(
        select(Clinic).where(Clinic.id == context.clinic_id).with_for_update()
    ).first()
    if clinic is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    actor_membership, _actor_user = _lock_active_admin(
        session,
        clinic_id=context.clinic_id,
        membership_id=context.membership.id,
    )
    row = session.exec(
        select(ClinicMembership, User)
        .join(User, col(User.id) == ClinicMembership.user_id)
        .where(
            ClinicMembership.id == membership_id,
            ClinicMembership.clinic_id == context.clinic_id,
        )
        .with_for_update()
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    membership, user = row
    if membership.id == actor_membership.id:
        raise HTTPException(status_code=409, detail="Admin cannot deactivate self")
    if membership.role == "admin" and membership.is_active:
        active_admins = session.exec(
            select(func.count())
            .select_from(ClinicMembership)
            .where(
                ClinicMembership.clinic_id == context.clinic_id,
                ClinicMembership.role == "admin",
                col(ClinicMembership.is_active).is_(True),
            )
        ).one()
        if active_admins <= 1:
            raise HTTPException(
                status_code=409,
                detail="Clinic must retain at least one active admin",
            )
    membership.is_active = False
    session.add(membership)
    now = get_datetime_utc()
    pending_invitations = session.exec(
        select(ClinicInvitation)
        .where(
            ClinicInvitation.clinic_id == context.clinic_id,
            col(ClinicInvitation.accepted_at).is_(None),
            col(ClinicInvitation.revoked_at).is_(None),
            or_(
                col(ClinicInvitation.email) == str(user.email).strip().lower(),
                col(ClinicInvitation.created_by_membership_id) == membership.id,
            ),
        )
        .with_for_update()
    ).all()
    for invitation in pending_invitations:
        invitation.revoked_at = now
        session.add(invitation)
    emit_change(
        session,
        context,
        action="membership.deactivated",
        resource_type="membership",
        resource_id=membership.id,
        metadata={"role": membership.role},
    )
    session.commit()
    session.refresh(membership)
    return _public(user, membership)


@router.get("/audit", response_model=AuditEventsPublic)
def audit_events(
    session: SessionDep,
    context: CurrentContext,
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditEventsPublic:
    _require_admin(context)
    events = session.exec(
        select(AuditEvent)
        .where(
            AuditEvent.clinic_id == context.clinic_id,
            AuditEvent.action != "fixture.loaded",
        )
        .order_by(col(AuditEvent.created_at).desc(), col(AuditEvent.id).desc())
        .limit(limit)
    ).all()
    data: list[AuditEventPublic] = []
    for event in events:
        value = event.metadata_json.get("version_id")
        try:
            version_id = uuid.UUID(value) if isinstance(value, str) else None
        except ValueError:
            version_id = None
        data.append(
            AuditEventPublic(
                id=event.id,
                actor_id=event.actor_id,
                action=event.action,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                version_id=version_id,
                created_at=event.created_at,
            )
        )
    return AuditEventsPublic(data=data, count=len(data))
