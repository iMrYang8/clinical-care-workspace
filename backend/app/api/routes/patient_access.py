"""Phone/OTP patient portal enrollment, sign-in, revocation, and recovery."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import func, text
from sqlmodel import Field, SQLModel, col, select
from sqlmodel._compat import SQLModelConfig

from app.api.deps import CurrentContext, SessionDep
from app.core import security
from app.core.config import settings
from app.core.db import set_rls_clinic, set_rls_patient_bootstrap
from app.models import (
    AuditEvent,
    Clinic,
    ClinicMembership,
    NotificationOutbox,
    NotificationState,
    PatientAccessCredential,
    PatientAccessEnrollStartRequest,
    PatientAccessLoginStartRequest,
    PatientAccessPublic,
    PatientAccessVerifyPublic,
    PatientAccessVerifyRequest,
    PatientOTPChallenge,
    PatientOTPChallengePublic,
    PatientOTPResendRequest,
    PatientPortalInvitation,
    PatientUserLink,
    ReasonCodeInput,
    Token,
    get_datetime_utc,
)
from app.services.messaging import NotificationChannelUnavailable
from app.services.nightingale import get_patient
from app.services.patient_access import (
    StartedOTPChallenge,
    challenge_clinic_id,
    dispatch_queued_access_notification,
    portal_clinic_code,
    provision_phone_access,
    resend_challenge,
    start_enrollment,
    start_login,
    verify_challenge,
)

router = APIRouter(prefix="/patient-access", tags=["patient-access"])
patient_router = APIRouter(prefix="/patients", tags=["patient-access"])


class PatientAccessProvisionCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    phone: str = Field(min_length=8, max_length=32)
    channel: Literal["sms", "whatsapp"] = "sms"


class PatientAccessProvisionPublic(SQLModel):
    access: PatientAccessPublic
    invitation_token: str
    claim_code: str
    claim_code_expires_at: datetime
    notification_id: uuid.UUID
    notification_state: NotificationState


class PatientAccessRevokeRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    reason_code: ReasonCodeInput = "access_revoked"


class PatientAccessRecoveryCreate(PatientAccessProvisionCreate):
    reason_code: ReasonCodeInput = "access_recovery"


def _require_registrar(context: CurrentContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Patient registrar role required")


def _access_public(credential: PatientAccessCredential) -> PatientAccessPublic:
    state: Literal["pending", "active", "revoked"]
    if credential.revoked_at is not None or not credential.is_active:
        state = "revoked"
    elif credential.user_id is None or credential.claim_code_used_at is None:
        state = "pending"
    else:
        state = "active"
    return PatientAccessPublic(
        credential_id=credential.id,
        patient_id=credential.patient_id,
        clinic_id=credential.clinic_id,
        portal_id=credential.portal_id,
        masked_phone=credential.masked_phone or "***",
        access_state=state,
    )


def _challenge_response(
    session: SessionDep,
    started: StartedOTPChallenge,
    *,
    delivery_state: NotificationState,
) -> PatientOTPChallengePublic:
    credential = session.get(PatientAccessCredential, started.challenge.credential_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="Patient access is unavailable")
    return PatientOTPChallengePublic(
        challenge_id=started.challenge.id,
        challenge_token=started.challenge_token,
        purpose=cast(
            Literal["enrollment", "login", "recovery", "phone_change"],
            started.challenge.purpose,
        ),
        portal_id=credential.portal_id,
        masked_phone=started.masked_phone,
        expires_at=started.challenge.expires_at,
        resend_available_at=started.challenge.resend_available_at,
        attempts_remaining=started.challenge.attempts_remaining,
        notification_id=started.notification_id,
        delivery_state=delivery_state,
    )


def _dispatch_after_commit(
    session: SessionDep, *, clinic_id: uuid.UUID, notification_id: uuid.UUID
) -> NotificationState:
    notification = dispatch_queued_access_notification(
        session, clinic_id=clinic_id, notification_id=notification_id
    )
    state = cast(NotificationState, notification.state)
    session.commit()
    return state


def _delivery_unavailable(_exc: NotificationChannelUnavailable) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": "PATIENT_DELIVERY_CHANNEL_UNAVAILABLE"},
    )


def _bind_patient_bootstrap(
    session: SessionDep,
    *,
    lookup_function: Literal[
        "app_lookup_patient_enrollment",
        "app_lookup_patient_portal",
        "app_lookup_patient_challenge",
    ],
    lookup_value: str,
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Resolve only opaque IDs through a locked-down PostgreSQL helper."""

    if session.get_bind().dialect.name != "postgresql":
        return None
    row = (
        session.connection()
        .execute(
            text(f"SELECT * FROM {lookup_function}(:lookup_value)"),
            {"lookup_value": lookup_value},
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    clinic_id = uuid.UUID(str(row.clinic_id))
    patient_id = uuid.UUID(str(row.patient_id))
    set_rls_clinic(session, clinic_id)
    # The SECURITY DEFINER lookup proved the opaque secret. Keep actor unset
    # until OTP verification resolves or creates the real linked identity.
    set_rls_patient_bootstrap(session, patient_id)
    return clinic_id, patient_id


@patient_router.post(
    "/{patient_id}/patient-access",
    response_model=PatientAccessProvisionPublic,
    status_code=201,
)
def provision_patient_access(
    patient_id: uuid.UUID,
    body: PatientAccessProvisionCreate,
    session: SessionDep,
    context: CurrentContext,
) -> PatientAccessProvisionPublic:
    """Create one revocable credential; reveal the claim code exactly once."""

    _require_registrar(context)
    patient = get_patient(session, context, patient_id)
    try:
        provisioned = provision_phone_access(
            session, context, patient, phone=body.phone, channel=body.channel
        )
    except NotificationChannelUnavailable as exc:
        raise _delivery_unavailable(exc) from exc
    session.add(
        AuditEvent(
            clinic_id=context.clinic_id,
            actor_id=context.user_id,
            action="patient.phone_access_provisioned",
            resource_type="patient_access",
            resource_id=provisioned.credential.id,
            reason_code="patient_access_requested",
            metadata_json={"channel": body.channel},
        )
    )
    credential_id = provisioned.credential.id
    notification_id = provisioned.notification_id
    invitation_token = provisioned.enrollment_token
    claim_code = provisioned.claim_code
    claim_expiry = provisioned.credential.claim_code_expires_at
    session.commit()
    notification_state = _dispatch_after_commit(
        session, clinic_id=context.clinic_id, notification_id=notification_id
    )
    credential = session.get(PatientAccessCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="Patient access is unavailable")
    return PatientAccessProvisionPublic(
        access=_access_public(credential),
        invitation_token=invitation_token,
        claim_code=claim_code,
        claim_code_expires_at=claim_expiry,
        notification_id=notification_id,
        notification_state=notification_state,
    )


def _revoke_active_access(
    session: SessionDep,
    context: CurrentContext,
    patient_id: uuid.UUID,
    *,
    reason_code: str,
    required: bool = True,
) -> PatientAccessCredential | None:
    credential = session.exec(
        select(PatientAccessCredential)
        .where(
            PatientAccessCredential.clinic_id == context.clinic_id,
            PatientAccessCredential.patient_id == patient_id,
            col(PatientAccessCredential.is_active).is_(True),
            col(PatientAccessCredential.revoked_at).is_(None),
        )
        .with_for_update()
    ).first()
    if credential is None:
        if required:
            raise HTTPException(
                status_code=404, detail="Active patient access not found"
            )
        return None
    now = get_datetime_utc()
    credential.is_active = False
    credential.revoked_at = now
    credential.updated_at = now
    credential.recovery_version += 1
    session.add(credential)
    challenges = session.exec(
        select(PatientOTPChallenge).where(
            PatientOTPChallenge.clinic_id == context.clinic_id,
            PatientOTPChallenge.credential_id == credential.id,
            col(PatientOTPChallenge.consumed_at).is_(None),
            col(PatientOTPChallenge.revoked_at).is_(None),
        )
    ).all()
    for challenge in challenges:
        challenge.revoked_at = now
        session.add(challenge)
    stale_delivery_keys = {
        hashlib.sha256(f"patient-enrollment:{credential.id}".encode()).hexdigest(),
        *(
            hashlib.sha256(f"patient-otp:{challenge.id}".encode()).hexdigest()
            for challenge in challenges
        ),
    }
    stale_deliveries = session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.clinic_id == context.clinic_id,
            col(NotificationOutbox.idempotency_key).in_(stale_delivery_keys),
            col(NotificationOutbox.state).in_(["queued", "submitted", "failed"]),
        )
    ).all()
    for notification in stale_deliveries:
        notification.state = "revoked"
        notification.revoked_at = now
        notification.updated_at = now
        session.add(notification)
    if credential.invitation_id is not None:
        invitation = session.get(PatientPortalInvitation, credential.invitation_id)
        if invitation is not None and invitation.revoked_at is None:
            invitation.revoked_at = now
            session.add(invitation)
    if credential.user_id is not None:
        membership = session.exec(
            select(ClinicMembership).where(
                ClinicMembership.clinic_id == context.clinic_id,
                ClinicMembership.user_id == credential.user_id,
                ClinicMembership.role == "patient",
            )
        ).first()
        if membership is not None:
            membership.is_active = False
            session.add(membership)
        links = session.exec(
            select(PatientUserLink).where(
                PatientUserLink.clinic_id == context.clinic_id,
                PatientUserLink.patient_id == patient_id,
                PatientUserLink.user_id == credential.user_id,
            )
        ).all()
        for link in links:
            session.delete(link)
    session.add(
        AuditEvent(
            clinic_id=context.clinic_id,
            actor_id=context.user_id,
            action="patient.phone_access_revoked",
            resource_type="patient_access",
            resource_id=credential.id,
            reason_code=reason_code,
            metadata_json={},
        )
    )
    session.flush()
    return credential


@patient_router.post(
    "/{patient_id}/patient-access/revoke", response_model=PatientAccessPublic
)
def revoke_patient_access(
    patient_id: uuid.UUID,
    body: PatientAccessRevokeRequest,
    session: SessionDep,
    context: CurrentContext,
) -> PatientAccessPublic:
    _require_registrar(context)
    get_patient(session, context, patient_id)
    credential = _revoke_active_access(
        session, context, patient_id, reason_code=body.reason_code
    )
    assert credential is not None
    session.commit()
    return _access_public(credential)


@patient_router.post(
    "/{patient_id}/patient-access/recover",
    response_model=PatientAccessProvisionPublic,
    status_code=201,
)
def recover_patient_access(
    patient_id: uuid.UUID,
    body: PatientAccessRecoveryCreate,
    session: SessionDep,
    context: CurrentContext,
) -> PatientAccessProvisionPublic:
    """Revoke the old credential and atomically issue a new phone claim."""

    _require_registrar(context)
    patient = get_patient(session, context, patient_id)
    latest_recovery_version = session.exec(
        select(func.max(PatientAccessCredential.recovery_version)).where(
            PatientAccessCredential.clinic_id == context.clinic_id,
            PatientAccessCredential.patient_id == patient_id,
        )
    ).one()
    revoked = _revoke_active_access(
        session,
        context,
        patient_id,
        reason_code=body.reason_code,
        required=False,
    )
    try:
        provisioned = provision_phone_access(
            session, context, patient, phone=body.phone, channel=body.channel
        )
    except NotificationChannelUnavailable as exc:
        raise _delivery_unavailable(exc) from exc
    provisioned.credential.recovery_version = max(
        int(latest_recovery_version or 1),
        revoked.recovery_version if revoked is not None else 1,
    )
    session.add(provisioned.credential)
    session.add(
        AuditEvent(
            clinic_id=context.clinic_id,
            actor_id=context.user_id,
            action="patient.phone_access_recovery_issued",
            resource_type="patient_access",
            resource_id=provisioned.credential.id,
            reason_code=body.reason_code,
            metadata_json={"channel": body.channel},
        )
    )
    credential_id = provisioned.credential.id
    notification_id = provisioned.notification_id
    session.commit()
    notification_state = _dispatch_after_commit(
        session, clinic_id=context.clinic_id, notification_id=notification_id
    )
    credential = session.get(PatientAccessCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="Patient access is unavailable")
    return PatientAccessProvisionPublic(
        access=_access_public(credential),
        invitation_token=provisioned.enrollment_token,
        claim_code=provisioned.claim_code,
        claim_code_expires_at=credential.claim_code_expires_at,
        notification_id=notification_id,
        notification_state=notification_state,
    )


@router.post(
    "/enroll/start",
    response_model=PatientOTPChallengePublic,
)
def begin_patient_enrollment(
    body: PatientAccessEnrollStartRequest, session: SessionDep
) -> PatientOTPChallengePublic:
    clinic_text, separator, _secret = body.invitation_token.partition(".")
    try:
        clinic_id = uuid.UUID(clinic_text) if separator else None
    except ValueError:
        clinic_id = None
    if clinic_id is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    bound = _bind_patient_bootstrap(
        session,
        lookup_function="app_lookup_patient_enrollment",
        lookup_value=hashlib.sha256(body.invitation_token.encode()).hexdigest(),
    )
    if bound is None:
        set_rls_clinic(session, clinic_id)
    elif bound[0] != clinic_id:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    try:
        started = start_enrollment(
            session,
            invitation_token=body.invitation_token,
            claim_code=body.claim_code,
            phone=body.phone,
        )
    except NotificationChannelUnavailable as exc:
        raise _delivery_unavailable(exc) from exc
    notification_id = started.notification_id
    session.commit()
    delivery_state = _dispatch_after_commit(
        session, clinic_id=clinic_id, notification_id=notification_id
    )
    return _challenge_response(session, started, delivery_state=delivery_state)


@router.post("/login/start", response_model=PatientOTPChallengePublic)
def begin_patient_login(
    body: PatientAccessLoginStartRequest, session: SessionDep
) -> PatientOTPChallengePublic:
    clinic_code = portal_clinic_code(body.portal_id)
    if clinic_code is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    bound = _bind_patient_bootstrap(
        session,
        lookup_function="app_lookup_patient_portal",
        lookup_value=body.portal_id.strip().upper(),
    )
    if bound is None:
        clinic = session.exec(select(Clinic).where(Clinic.code == clinic_code)).first()
        if clinic is None:
            raise HTTPException(status_code=400, detail="Patient access is invalid")
        set_rls_clinic(session, clinic.id)
        clinic_id = clinic.id
    else:
        clinic_id = bound[0]
        clinic = session.get(Clinic, clinic_id)
        if clinic is None or clinic.code != clinic_code:
            raise HTTPException(status_code=400, detail="Patient access is invalid")
    try:
        started = start_login(session, portal_id=body.portal_id)
    except NotificationChannelUnavailable as exc:
        raise _delivery_unavailable(exc) from exc
    notification_id = started.notification_id
    session.commit()
    delivery_state = _dispatch_after_commit(
        session, clinic_id=clinic_id, notification_id=notification_id
    )
    return _challenge_response(session, started, delivery_state=delivery_state)


@router.post("/resend", response_model=PatientOTPChallengePublic)
def resend_patient_otp(
    body: PatientOTPResendRequest, session: SessionDep
) -> PatientOTPChallengePublic:
    clinic_id = challenge_clinic_id(body.challenge_token)
    if clinic_id is None:
        raise HTTPException(status_code=400, detail="OTP is invalid or expired")
    bound = _bind_patient_bootstrap(
        session,
        lookup_function="app_lookup_patient_challenge",
        lookup_value=hashlib.sha256(body.challenge_token.encode()).hexdigest(),
    )
    if bound is None:
        set_rls_clinic(session, clinic_id)
    elif bound[0] != clinic_id:
        raise HTTPException(status_code=400, detail="OTP is invalid or expired")
    try:
        started = resend_challenge(session, challenge_token=body.challenge_token)
    except NotificationChannelUnavailable as exc:
        raise _delivery_unavailable(exc) from exc
    notification_id = started.notification_id
    session.commit()
    delivery_state = _dispatch_after_commit(
        session, clinic_id=clinic_id, notification_id=notification_id
    )
    return _challenge_response(session, started, delivery_state=delivery_state)


@router.post("/verify", response_model=PatientAccessVerifyPublic)
def verify_patient_otp(
    body: PatientAccessVerifyRequest,
    response: Response,
    session: SessionDep,
) -> PatientAccessVerifyPublic:
    clinic_id = challenge_clinic_id(body.challenge_token)
    if clinic_id is None:
        raise HTTPException(status_code=400, detail="OTP is invalid or expired")
    bound = _bind_patient_bootstrap(
        session,
        lookup_function="app_lookup_patient_challenge",
        lookup_value=hashlib.sha256(body.challenge_token.encode()).hexdigest(),
    )
    if bound is None:
        set_rls_clinic(session, clinic_id)
    elif bound[0] != clinic_id:
        raise HTTPException(status_code=400, detail="OTP is invalid or expired")
    user, membership, credential = verify_challenge(
        session, challenge_token=body.challenge_token, otp=body.otp
    )
    token = Token(
        access_token=security.create_access_token(
            user.id,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            membership_id=membership.id,
            clinic_id=membership.clinic_id,
        )
    )
    session.commit()
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token.access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )
    return PatientAccessVerifyPublic(access=_access_public(credential), token=token)
