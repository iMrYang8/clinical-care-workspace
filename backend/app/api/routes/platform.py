from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

import jwt
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.api.deps import RequestContext, SessionDep
from app.api.routes.patient_registry import patient_detail
from app.core import security
from app.core.config import settings
from app.core.db import set_rls_clinic
from app.models import (
    Clinic,
    ClinicMembership,
    Patient,
    PatientDetailPublic,
    PatientTimeline,
    PlatformAdministrator,
    PlatformAuditEvent,
    PlatformAuditPublic,
    PlatformAuditsPublic,
    PlatformClinicPublic,
    PlatformClinicsPublic,
    PlatformLogin,
    PlatformMePublic,
    Token,
    TokenPayload,
    User,
)
from app.services.nightingale import timeline

router = APIRouter(prefix="/platform", tags=["platform"])


@dataclass(frozen=True)
class PlatformContext:
    user: User
    administrator: PlatformAdministrator


def _platform_context(
    session: SessionDep,
    token: Annotated[
        str | None, Cookie(alias=settings.PLATFORM_AUTH_COOKIE_NAME)
    ] = None,
) -> PlatformContext:
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
            options={"require": ["exp"]},
        )
        token_data = TokenPayload(**payload)
        user_id = uuid.UUID(token_data.sub or "")
        administrator_id = uuid.UUID(token_data.platform_admin_id or "")
    except (InvalidTokenError, ValidationError, ValueError):
        raise HTTPException(status_code=403, detail="Could not validate credentials")
    if token_data.scope != "platform":
        raise HTTPException(status_code=403, detail="Invalid platform scope")
    user = session.get(User, user_id)
    administrator = session.get(PlatformAdministrator, administrator_id)
    if (
        user is None
        or administrator is None
        or administrator.user_id != user.id
        or not user.is_active
        or not administrator.is_active
    ):
        raise HTTPException(status_code=403, detail="Inactive platform account")
    return PlatformContext(user=user, administrator=administrator)


PlatformContextDep = Annotated[PlatformContext, Depends(_platform_context)]


def _audit(
    session: Session,
    context: PlatformContext,
    *,
    action: str,
    request_id: str | None,
    clinic_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
) -> None:
    session.add(
        PlatformAuditEvent(
            platform_admin_id=context.administrator.id,
            action=action,
            target_clinic_id=clinic_id,
            target_patient_id=patient_id,
            request_id=request_id or str(uuid.uuid4()),
            metadata_json={},
        )
    )


def _clinic_by_code(session: Session, clinic_code: str) -> Clinic:
    clinic = session.exec(
        select(Clinic).where(Clinic.code == clinic_code.strip().upper())
    ).first()
    if clinic is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


@router.post("/auth/login", response_model=Token)
def platform_login(
    body: PlatformLogin, response: Response, session: SessionDep
) -> Token:
    normalized_email = str(body.email).strip().lower()
    user = session.exec(select(User).where(User.email == normalized_email)).first()
    administrator = (
        session.exec(
            select(PlatformAdministrator).where(
                PlatformAdministrator.user_id == user.id,
                col(PlatformAdministrator.is_active).is_(True),
            )
        ).first()
        if user is not None
        else None
    )
    valid = False
    if user is not None and administrator is not None and user.is_active:
        valid, _ = security.verify_password(body.password, user.hashed_password)
    if not valid or user is None or administrator is None:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    access_token = security.create_access_token(
        user.id,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        platform_admin_id=administrator.id,
        scope="platform",
    )
    response.set_cookie(
        key=settings.PLATFORM_AUTH_COOKIE_NAME,
        value=access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )
    return Token(access_token=access_token)


@router.post("/auth/logout", status_code=204)
def platform_logout(response: Response) -> None:
    response.delete_cookie(
        key=settings.PLATFORM_AUTH_COOKIE_NAME,
        path="/",
        secure=settings.AUTH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )


@router.get("/auth/me", response_model=PlatformMePublic)
def platform_me(context: PlatformContextDep) -> PlatformMePublic:
    return PlatformMePublic(
        user_id=context.user.id,
        platform_admin_id=context.administrator.id,
        email=context.user.email,
        full_name=context.user.full_name,
    )


@router.get("/clinics", response_model=PlatformClinicsPublic)
def platform_clinics(
    session: SessionDep,
    context: PlatformContextDep,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> PlatformClinicsPublic:
    clinics = list(session.exec(select(Clinic).order_by(Clinic.name)).all())
    data: list[PlatformClinicPublic] = []
    for clinic in clinics:
        set_rls_clinic(session, clinic.id)
        member_count = session.exec(
            select(func.count())
            .select_from(ClinicMembership)
            .where(
                ClinicMembership.clinic_id == clinic.id,
                col(ClinicMembership.is_active).is_(True),
            )
        ).one()
        patient_count = session.exec(
            select(func.count())
            .select_from(Patient)
            .where(
                Patient.clinic_id == clinic.id,
                Patient.status == "active",
            )
        ).one()
        data.append(
            PlatformClinicPublic(
                id=clinic.id,
                code=clinic.code,
                name=clinic.name,
                member_count=member_count,
                patient_count=patient_count,
            )
        )
    _audit(session, context, action="platform.clinics.viewed", request_id=request_id)
    session.commit()
    return PlatformClinicsPublic(data=data, count=len(data))


@router.get("/clinics/{clinic_code}/patients", response_model=list[PatientDetailPublic])
def platform_patients(
    clinic_code: str,
    session: SessionDep,
    context: PlatformContextDep,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> list[PatientDetailPublic]:
    clinic = _clinic_by_code(session, clinic_code)
    set_rls_clinic(session, clinic.id)
    patients = session.exec(
        select(Patient)
        .where(Patient.clinic_id == clinic.id)
        .order_by(col(Patient.created_at))
    ).all()
    output = [patient_detail(session, patient) for patient in patients]
    _audit(
        session,
        context,
        action="platform.patients.viewed",
        request_id=request_id,
        clinic_id=clinic.id,
    )
    session.commit()
    return output


@router.get(
    "/clinics/{clinic_code}/patients/{patient_id}/timeline",
    response_model=PatientTimeline,
)
def platform_patient_timeline(
    clinic_code: str,
    patient_id: uuid.UUID,
    session: SessionDep,
    context: PlatformContextDep,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> PatientTimeline:
    clinic = _clinic_by_code(session, clinic_code)
    set_rls_clinic(session, clinic.id)
    detached_membership = ClinicMembership(
        id=uuid.uuid4(), clinic_id=clinic.id, user_id=context.user.id, role="admin"
    )
    clinical_context = RequestContext(user=context.user, membership=detached_membership)
    data = timeline(session, clinical_context, patient_id)
    _audit(
        session,
        context,
        action="platform.patient_timeline.viewed",
        request_id=request_id,
        clinic_id=clinic.id,
        patient_id=patient_id,
    )
    session.commit()
    return PatientTimeline(data=data, count=len(data))


@router.get("/audit", response_model=PlatformAuditsPublic)
def platform_audit_log(
    session: SessionDep, context: PlatformContextDep
) -> PlatformAuditsPublic:
    del context
    rows = session.exec(
        select(PlatformAuditEvent).order_by(col(PlatformAuditEvent.created_at).desc())
    ).all()
    data = [PlatformAuditPublic.model_validate(row) for row in rows]
    return PlatformAuditsPublic(data=data, count=len(data))
