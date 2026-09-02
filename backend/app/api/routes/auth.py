import hashlib
import uuid
from datetime import timedelta
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app import crud
from app.api.deps import CurrentContext, SessionDep
from app.clinic_codes import normalize_clinic_code
from app.core import security
from app.core.config import settings
from app.core.db import (
    set_rls_actor,
    set_rls_clinic,
    set_rls_invitation_token_hash,
)
from app.models import (
    AuditEvent,
    Clinic,
    ClinicInvitation,
    ClinicMembership,
    DemoLoginRequest,
    MembershipInvitationAccept,
    MembershipPublic,
    MePublic,
    Message,
    PlatformAdministrator,
    Role,
    Token,
    User,
    get_datetime_utc,
)
from app.seed import demo_id, membership_for_persona

router = APIRouter(prefix="/auth", tags=["auth"])

_AUTHENTICATION_ERROR = "Incorrect clinic code, email, or password"


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
    set_rls_actor(session, demo_id(f"user-{body.persona}"))
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
    x_clinic_code: Annotated[str, Header(alias="X-Clinic-Code")],
) -> Token:
    clinic_code = normalize_clinic_code(x_clinic_code)
    if clinic_code is None or not 1 <= len(form_data.password) <= 200:
        security.verify_password(form_data.password, crud.DUMMY_HASH)
        raise HTTPException(status_code=400, detail=_AUTHENTICATION_ERROR)
    clinic = session.exec(select(Clinic).where(Clinic.code == clinic_code)).first()
    if clinic is None:
        security.verify_password(form_data.password, crud.DUMMY_HASH)
        raise HTTPException(status_code=400, detail=_AUTHENTICATION_ERROR)
    _set_rls_clinic(session, clinic.id)
    normalized_email = form_data.username.strip().lower()
    if session.get_bind().dialect.name == "postgresql":
        user_id = (
            session.connection()
            .execute(
                text("SELECT app_lookup_clinic_user(:clinic_code, :email)"),
                {"clinic_code": clinic_code, "email": normalized_email},
            )
            .scalar_one_or_none()
        )
        if user_id is not None:
            set_rls_actor(session, uuid.UUID(str(user_id)))
            user = session.get(User, uuid.UUID(str(user_id)))
        else:
            user = None
            security.verify_password(form_data.password, crud.DUMMY_HASH)
        if (
            user is not None
            and user.account_kind in {"staff", "patient"}
            and user.email is not None
            and user.hashed_password is not None
        ):
            verified, updated_hash = security.verify_password(
                form_data.password, user.hashed_password
            )
            if not verified:
                user = None
            elif updated_hash:
                user.hashed_password = updated_hash
                session.add(user)
        elif user is not None:
            security.verify_password(form_data.password, crud.DUMMY_HASH)
            user = None
    else:
        user = crud.authenticate(
            session=session, email=normalized_email, password=form_data.password
        )
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail=_AUTHENTICATION_ERROR)
    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.user_id == user.id,
            ClinicMembership.clinic_id == clinic.id,
            col(ClinicMembership.is_active).is_(True),
        )
    ).first()
    # Worker memberships are service identities bound to a claimed job. They
    # are deliberately excluded from the human password-login surface; worker
    # tokens continue to be issued only by the trusted internal workflow.
    if membership is None or membership.role == "worker":
        raise HTTPException(status_code=400, detail=_AUTHENTICATION_ERROR)
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
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    normalized_email = str(body.email).strip().lower()
    now = get_datetime_utc()
    _set_rls_clinic(session, clinic_id)
    invitation: ClinicInvitation | None = None
    invitation_id: uuid.UUID
    invitation_role: str
    invited_full_name: str | None = None
    if session.get_bind().dialect.name == "postgresql":
        bootstrap = (
            session.connection()
            .execute(
                text(
                    "SELECT * FROM app_lookup_clinic_invitation("
                    ":clinic_id, :token_hash, :email)"
                ),
                {
                    "clinic_id": clinic_id,
                    "token_hash": token_hash,
                    "email": normalized_email,
                },
            )
            .one_or_none()
        )
        if bootstrap is None:
            raise HTTPException(
                status_code=400, detail="Invitation is invalid or expired"
            )
        invitation_id = uuid.UUID(str(bootstrap.invitation_id))
        invitation_role = str(bootstrap.role)
        prospective_user_id = (
            uuid.UUID(str(bootstrap.existing_user_id))
            if bootstrap.existing_user_id is not None
            else uuid.uuid4()
        )
        set_rls_invitation_token_hash(session, token_hash)
    else:
        invitation = session.exec(
            select(ClinicInvitation)
            .where(
                ClinicInvitation.clinic_id == clinic_id,
                ClinicInvitation.token_hash == token_hash,
            )
            .with_for_update()
        ).first()
        creator_is_valid = False
        if invitation is not None and invitation.created_by_membership_id is not None:
            inviter_row = session.exec(
                select(ClinicMembership, User)
                .join(User, col(User.id) == ClinicMembership.user_id)
                .where(
                    ClinicMembership.clinic_id == clinic_id,
                    ClinicMembership.id == invitation.created_by_membership_id,
                )
            ).first()
            if inviter_row is not None:
                inviter_membership, inviter_user = inviter_row
                creator_is_valid = bool(
                    inviter_membership.is_active
                    and inviter_membership.role == "admin"
                    and inviter_user.is_active
                )
        elif invitation is not None and invitation.created_by_platform_admin_id:
            platform_creator = session.get(
                PlatformAdministrator, invitation.created_by_platform_admin_id
            )
            creator_is_valid = bool(platform_creator and platform_creator.is_active)
        if (
            invitation is None
            or invitation.accepted_at is not None
            or invitation.revoked_at is not None
            or invitation.expires_at <= now
            or invitation.role not in {"staff", "clinician", "admin"}
            or str(invitation.email).strip().lower() != normalized_email
            or not creator_is_valid
        ):
            raise HTTPException(
                status_code=400, detail="Invitation is invalid or expired"
            )
        invitation_id = invitation.id
        invitation_role = invitation.role
        invited_full_name = invitation.invited_full_name
        existing_user = session.exec(
            select(User).where(User.email == normalized_email)
        ).first()
        prospective_user_id = (
            existing_user.id if existing_user is not None else uuid.uuid4()
        )
    if invitation_role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    set_rls_actor(session, prospective_user_id, role=invitation_role)

    clinic = session.get(Clinic, clinic_id)
    if clinic is None:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")

    user = session.get(User, prospective_user_id)
    if user is None:
        user = User(
            id=prospective_user_id,
            email=normalized_email,
            full_name=body.full_name or invited_full_name,
            hashed_password=security.get_password_hash(body.password),
            account_kind="staff",
        )
        session.add(user)
        session.flush()
    else:
        # Possession of the secret delivered to this email is the identity
        # verification step. Replacing the password evicts an attacker who
        # globally pre-registered someone else's address.
        if (
            user.account_kind != "staff"
            or not user.is_active
            or user.email is None
            or str(user.email).strip().lower() != normalized_email
        ):
            raise HTTPException(
                status_code=400, detail="Invitation is invalid or expired"
            )
        user.hashed_password = security.get_password_hash(body.password)
        if body.full_name is not None:
            user.full_name = body.full_name
        session.add(user)
        session.flush()

    membership = session.exec(
        select(ClinicMembership)
        .where(
            ClinicMembership.clinic_id == clinic_id,
            ClinicMembership.user_id == user.id,
        )
        .with_for_update()
    ).first()
    # Invitation acceptance never reactivates a membership. Deactivation is an
    # explicit security boundary; a new admin-reviewed workflow must create a
    # fresh invitation after the inactive membership is handled separately.
    if membership is not None:
        raise HTTPException(status_code=409, detail="Clinic membership already exists")
    membership = ClinicMembership(
        clinic_id=clinic_id,
        user_id=user.id,
        role=invitation_role,
    )
    try:
        with session.begin_nested():
            session.add(membership)
            session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="Clinic membership already exists"
        ) from exc

    if invitation is None:
        invitation = session.exec(
            select(ClinicInvitation)
            .where(
                ClinicInvitation.clinic_id == clinic_id,
                ClinicInvitation.id == invitation_id,
                ClinicInvitation.token_hash == token_hash,
            )
            .with_for_update()
        ).first()
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
    ):
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")

    invitation.accepted_at = now
    session.add(invitation)
    session.add(
        AuditEvent(
            clinic_id=clinic_id,
            actor_id=user.id,
            action="membership.invitation_accepted",
            resource_type="membership",
            resource_id=membership.id,
            reason_code="invitation_accepted",
            metadata_json={"role": invitation_role},
        )
    )
    session.commit()
    session.refresh(membership)
    if user.email is None:
        raise HTTPException(status_code=409, detail="Staff email is unavailable")
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
def me(session: SessionDep, context: CurrentContext) -> MePublic:
    clinic = session.get(Clinic, context.clinic_id)
    if clinic is None:
        raise HTTPException(status_code=403, detail="Invalid membership context")
    return MePublic(
        user_id=context.user_id,
        email=context.user.email,
        full_name=context.user.full_name,
        clinic_id=context.clinic_id,
        clinic_code=clinic.code,
        clinic_name=clinic.name,
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
