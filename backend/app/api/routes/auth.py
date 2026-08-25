import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import text
from sqlmodel import col, select

from app import crud
from app.api.deps import CurrentContext, SessionDep
from app.core import security
from app.core.config import settings
from app.models import (
    ClinicMembership,
    DemoLoginRequest,
    MePublic,
    Message,
    Token,
)
from app.seed import demo_id, membership_for_persona

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_rls_clinic(session: SessionDep, clinic_id: uuid.UUID) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT set_config('app.current_clinic_id', :clinic_id, true)"),
            {"clinic_id": str(clinic_id)},
        )


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


@router.post("/demo-login", response_model=Token)
def demo_login(body: DemoLoginRequest, session: SessionDep) -> Token:
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
    return _token(membership, job_id=trusted_job_id)


@router.post("/login", response_model=Token)
def password_login(
    session: SessionDep,
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
    return _token(membership)


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
def logout(context: CurrentContext) -> Message:
    # JWTs are stateless; clients discard the token. No user-controlled context is used.
    return Message(message=f"Logged out membership {context.membership.id}")
