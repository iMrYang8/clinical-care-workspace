import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import cast

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    AuditEvent,
    AuditEventPublic,
    AuditEventsPublic,
    Clinic,
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


def _require_admin(context: CurrentContext) -> None:
    if context.role != "admin":
        raise HTTPException(status_code=403, detail="Clinic admin role required")


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
    pending = session.exec(
        select(ClinicInvitation).where(
            ClinicInvitation.clinic_id == context.clinic_id,
            ClinicInvitation.email == normalized_email,
            col(ClinicInvitation.accepted_at).is_(None),
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
        created_by_membership_id=context.membership.id,
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
    try:
        deliver_membership_invitation(
            recipient=normalized_email,
            token=raw_token,
        )
    except Exception:
        # No token, existing-user fact, or SMTP detail is returned to the admin.
        session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Invitation delivery did not complete",
        )
    session.commit()
    session.refresh(invitation)
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
    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.id == membership_id,
            ClinicMembership.clinic_id == context.clinic_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    if membership.id == context.membership.id:
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
    user = session.get(User, membership.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    membership.is_active = False
    session.add(membership)
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
        .where(AuditEvent.clinic_id == context.clinic_id)
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
