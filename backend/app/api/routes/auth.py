import hashlib
import uuid
from datetime import timedelta
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import col, select

from app import crud
from app.api.deps import CurrentContext, SessionDep
from app.core import security
from app.core.config import settings
from app.core.db import set_rls_clinic
from app.models import (
    AuditEvent,
    ClinicInvitation,
    ClinicMembership,
    DemoLoginRequest,
    MembershipInvitationAccept,
    MembershipPublic,
    MePublic,
    Message,
    Role,
    Token,
    User,
    get_datetime_utc,
)
from app.seed import demo_id, membership_for_persona

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_rls_clinic(session: SessionDep, clinic_id: uuid.UUID) -> None:
    set_rls_clinic(session, clinic_id)


def _token(membership: ClinicMembership, *, job_id: str | None = None) -> Token:
    return Token(
        access_token=security.create_access_token(
            membership.user_id,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            membership_id=membership.id,
            clinic_id=membership.clinic_id,
            job_id=job_id,
        )
    )


def _set_browser_cookie(response: Response, token: Token) -> None:
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token.access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )


@router.post("/demo-login", response_model=Token)
def demo_login(
    body: DemoLoginRequest, response: Response, session: SessionDep
) -> Token:
    """Map one fixed synthetic persona to its server-owned membership."""

    if settings.FASTAPI_ENV != "development" or not settings.ENABLE_DEMO_AUTH:
        raise HTTPException(status_code=404, detail="Not found")
    trusted_clinic_id = demo_id(
        "clinic-other" if body.persona == "other_staff" else "clinic-primary"
    )
    _set_rls_clinic(session, trusted_clinic_id)
    membership = membership_for_persona(session, body.persona)
    if membership is None:
        raise HTTPException(status_code=404, detail="Demo persona not seeded")
    trusted_job_id = (
        str(demo_id("job-worker-demo")) if body.persona == "worker" else None
    )
    token = _token(membership, job_id=trusted_job_id)
    _set_browser_cookie(response, token)
    return token


@router.post("/login", response_model=Token)
def password_login(
    session: SessionDep,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    x_clinic_id: Annotated[uuid.UUID, Header(alias="X-Clinic-ID")],
) -> Token:
    user = crud.authenticate(
        session=session, email=form_data.username, password=form_data.password
    )
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    _set_rls_clinic(session, x_clinic_id)
    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.user_id == user.id,
            ClinicMembership.clinic_id == x_clinic_id,
            col(ClinicMembership.is_active).is_(True),
        )
    ).first()
    if membership is None:
        raise HTTPException(status_code=403, detail="No active clinic membership")
    token = _token(membership)
    _set_browser_cookie(response, token)
    return token


@router.post("/invitations/accept", response_model=MembershipPublic)
def accept_membership_invitation(
    body: MembershipInvitationAccept,
    session: SessionDep,
) -> MembershipPublic:
    """Verify one emailed secret before binding a global identity to a clinic."""

    clinic_text, separator, _secret = body.token.partition(".")
    if not separator:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    try:
        clinic_id = uuid.UUID(clinic_text)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    _set_rls_clinic(session, clinic_id)
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    invitation = session.exec(
        select(ClinicInvitation)
        .where(
            ClinicInvitation.clinic_id == clinic_id,
            ClinicInvitation.token_hash == token_hash,
        )
        .with_for_update()
    ).first()
    now = get_datetime_utc()
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.expires_at <= now
    ):
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")

    normalized_email = str(invitation.email).strip().lower()
    user = session.exec(select(User).where(User.email == normalized_email)).first()
    if user is None:
        user = User(
            email=normalized_email,
            full_name=body.full_name or invitation.invited_full_name,
            hashed_password=security.get_password_hash(body.password),
        )
        session.add(user)
        session.flush()
    else:
        # Possession of the secret delivered to this email is the identity
        # verification step. Replacing the password evicts an attacker who
        # globally pre-registered someone else's address.
        user.hashed_password = security.get_password_hash(body.password)
        user.is_active = True
        if body.full_name is not None:
            user.full_name = body.full_name
        session.add(user)
        session.flush()

    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == clinic_id,
            ClinicMembership.user_id == user.id,
        )
    ).first()
    if membership is not None and membership.is_active:
        raise HTTPException(status_code=409, detail="Active membership already exists")
    if membership is None:
        membership = ClinicMembership(
            clinic_id=clinic_id,
            user_id=user.id,
            role=invitation.role,
        )
    else:
        membership.role = invitation.role
        membership.is_active = True
    session.add(membership)
    session.flush()

    invitation.accepted_at = now
    session.add(invitation)
    session.add(
        AuditEvent(
            clinic_id=clinic_id,
            actor_id=user.id,
            action="membership.invitation_accepted",
            resource_type="membership",
            resource_id=membership.id,
            metadata_json={"role": invitation.role},
        )
    )
    session.commit()
    session.refresh(membership)
    return MembershipPublic(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=cast(Role, membership.role),
        is_active=membership.is_active,
        created_at=membership.created_at,
    )


@router.get("/me", response_model=MePublic)
def me(context: CurrentContext) -> MePublic:
    return MePublic(
        user_id=context.user_id,
        email=context.user.email,
        full_name=context.user.full_name,
        clinic_id=context.clinic_id,
        membership_id=context.membership.id,
        role=context.role,
    )


@router.post("/logout", response_model=Message)
def logout(response: Response) -> Message:
    """Idempotently clear the browser credential, even when it is stale.

    Cookie-authenticated calls still pass the same-origin CSRF middleware. The
    route intentionally does not resolve a membership: an expired or corrupt
    cookie must never trap a user on a shared device.
    """

    response.delete_cookie(
        settings.AUTH_COOKIE_NAME,
        path="/",
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )
    return Message(message="Browser session cookie cleared")
