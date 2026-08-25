import uuid
from typing import cast

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.core.security import get_password_hash
from app.models import (
    AuditEvent,
    AuditEventPublic,
    AuditEventsPublic,
    ClinicMembership,
    MembershipCreate,
    MembershipPublic,
    MembershipsPublic,
    Role,
    User,
)
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


@router.post("/memberships", status_code=201, response_model=MembershipPublic)
def create_membership(
    body: MembershipCreate, session: SessionDep, context: CurrentContext
) -> MembershipPublic:
    _require_admin(context)
    user = session.exec(select(User).where(User.email == body.email)).first()
    if user is None:
        user = User(
            email=body.email,
            full_name=body.full_name,
            hashed_password=get_password_hash(body.temporary_password),
        )
        session.add(user)
        session.flush()
    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == context.clinic_id,
            ClinicMembership.user_id == user.id,
        )
    ).first()
    if membership is not None and membership.is_active:
        raise HTTPException(status_code=409, detail="Membership already exists")
    if membership is None:
        membership = ClinicMembership(
            clinic_id=context.clinic_id,
            user_id=user.id,
            role=body.role,
        )
    else:
        membership.is_active = True
        membership.role = body.role
    session.add(membership)
    session.flush()
    emit_change(
        session,
        context,
        action="membership.created",
        resource_type="membership",
        resource_id=membership.id,
        metadata={"role": body.role},
    )
    session.commit()
    session.refresh(membership)
    return _public(user, membership)


@router.post("/memberships/{membership_id}/deactivate", response_model=MembershipPublic)
def deactivate_membership(
    membership_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> MembershipPublic:
    _require_admin(context)
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
