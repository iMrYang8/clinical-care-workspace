"""Minimal clinic-scoped directory used for discussion and assignment pickers."""

from typing import cast

from fastapi import APIRouter, HTTPException
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    ClinicMembership,
    MembershipRole,
    TeamMemberPublic,
    TeamMembersPublic,
    User,
)

router = APIRouter(prefix="/team", tags=["team"])

_CARE_TEAM_ROLES = {"staff", "clinician", "admin"}


@router.get("/members", response_model=TeamMembersPublic)
def team_members(session: SessionDep, context: CurrentContext) -> TeamMembersPublic:
    if context.role not in _CARE_TEAM_ROLES:
        raise HTTPException(status_code=403, detail="Care team role required")
    rows = session.exec(
        select(ClinicMembership, User)
        .join(User, col(User.id) == ClinicMembership.user_id)
        .where(
            ClinicMembership.clinic_id == context.clinic_id,
            col(ClinicMembership.is_active).is_(True),
            col(User.is_active).is_(True),
            col(ClinicMembership.role).in_(_CARE_TEAM_ROLES),
        )
        .order_by(col(User.full_name), col(ClinicMembership.id))
    ).all()
    data = [
        TeamMemberPublic(
            membership_id=membership.id,
            user_id=user.id,
            full_name=user.full_name,
            role=cast(MembershipRole, membership.role),
        )
        for membership, user in rows
    ]
    return TeamMembersPublic(data=data, count=len(data))
