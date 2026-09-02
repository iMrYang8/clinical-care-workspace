"""Phone/OTP patient portal access without treating a phone number as identity."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
from sqlmodel import Session, col, func, select

from app.api.deps import RequestContext
from app.core import security
from app.core.config import settings
from app.core.db import set_rls_actor
from app.core.field_crypto import field_codec
from app.models import (
    AuditEvent,
    Clinic,
    ClinicMembership,
    NotificationOutbox,
    Patient,
    PatientAccessCredential,
    PatientOTPChallenge,
    PatientPortalInvitation,
    PatientUserLink,
    User,
    get_datetime_utc,
)
from app.services.messaging import (
    bind_notification_worker,
    dispatch_notification,
    queue_notification,
)

_PORTAL_ID = re.compile(r"^(?P<clinic>[A-Z]{3,12})-[A-Z0-9]{8}$")


@dataclass(frozen=True)
class ProvisionedPatientAccess:
    credential: PatientAccessCredential
    invitation: PatientPortalInvitation
    claim_code: str
    enrollment_token: str
    notification_id: uuid.UUID


@dataclass(frozen=True)
class StartedOTPChallenge:
    challenge: PatientOTPChallenge
    challenge_token: str
    otp: str
    masked_phone: str
    notification_id: uuid.UUID


def normalize_phone(value: str) -> str:
    candidate = value.strip().replace(" ", "").replace("-", "")
    if candidate.startswith("00"):
        candidate = f"+{candidate[2:]}"
    if candidate.isdigit() and len(candidate) == 8:
        candidate = f"+65{candidate}"
    if not re.fullmatch(r"\+[1-9]\d{7,14}", candidate):
        raise HTTPException(status_code=422, detail="Phone must use E.164 format")
    return candidate


def portal_clinic_code(portal_id: str) -> str | None:
    match = _PORTAL_ID.fullmatch(portal_id.strip().upper())
    return match.group("clinic") if match else None


def challenge_clinic_id(challenge_token: str) -> uuid.UUID | None:
    clinic_text, separator, _ = challenge_token.partition(".")
    if not separator:
        return None
    try:
        return uuid.UUID(clinic_text)
    except ValueError:
        return None


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _claim_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _portal_id(clinic_code: str) -> str:
    return f"{clinic_code}-{secrets.token_hex(4).upper()}"


def provision_phone_access(
    session: Session,
    context: RequestContext,
    patient: Patient,
    *,
    phone: str,
    channel: str,
) -> ProvisionedPatientAccess:
    if channel not in {"sms", "whatsapp"}:
        raise HTTPException(status_code=422, detail="Phone channel is invalid")
    normalized_phone = normalize_phone(phone)
    active = session.exec(
        select(PatientAccessCredential).where(
            PatientAccessCredential.clinic_id == context.clinic_id,
            PatientAccessCredential.patient_id == patient.id,
            col(PatientAccessCredential.is_active).is_(True),
            col(PatientAccessCredential.revoked_at).is_(None),
        )
    ).first()
    if active is not None:
        raise HTTPException(status_code=409, detail="Patient access already active")
    now = get_datetime_utc()
    invitation_id = uuid.uuid4()
    invitation_secret = secrets.token_urlsafe(32)
    enrollment_token = f"{context.clinic_id}.{invitation_secret}"
    invitation = PatientPortalInvitation(
        id=invitation_id,
        clinic_id=context.clinic_id,
        patient_id=patient.id,
        email=None,
        token_hash=_token_hash(enrollment_token),
        created_by_membership_id=context.membership.id,
        expires_at=now + timedelta(days=settings.PATIENT_CLAIM_TTL_DAYS),
    )
    session.add(invitation)
    claim_code = _claim_code()
    credential_id = uuid.uuid4()
    clinic = session.get(Clinic, context.clinic_id)
    if clinic is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    credential = PatientAccessCredential(
        id=credential_id,
        clinic_id=context.clinic_id,
        patient_id=patient.id,
        invitation_id=invitation.id,
        portal_id=_portal_id(clinic.code),
        phone_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "patient_access.phone",
            credential_id,
            normalized_phone,
        ),
        phone_hmac=field_codec.blind_index(
            context.clinic_id, "patient_access.phone", normalized_phone
        ),
        masked_phone=f"***{normalized_phone[-4:]}",
        preferred_channel=channel,
        claim_code_hash=_token_hash(claim_code),
        claim_code_expires_at=now + timedelta(days=settings.PATIENT_CLAIM_TTL_DAYS),
        created_by_membership_id=context.membership.id,
    )
    session.add(credential)
    session.flush()
    notification, _ = queue_notification(
        session,
        clinic_id=context.clinic_id,
        patient_id=patient.id,
        purpose="patient_enrollment",
        channel=channel,
        destination=normalized_phone,
        template_key="patient-enrollment-v1",
        payload={
            "portal_id": credential.portal_id,
            "enrollment_token": enrollment_token,
        },
        idempotency_key=f"patient-enrollment:{credential.id}",
        portal_invitation_id=invitation.id,
        created_by_membership_id=context.membership.id,
    )
    session.flush()
    return ProvisionedPatientAccess(
        credential=credential,
        invitation=invitation,
        claim_code=claim_code,
        enrollment_token=enrollment_token,
        notification_id=notification.id,
    )


def _credential_phone(credential: PatientAccessCredential) -> str:
    if credential.phone_ciphertext is None:
        raise HTTPException(status_code=409, detail="Phone access is unavailable")
    return field_codec.decrypt_text(
        credential.clinic_id,
        "patient_access.phone",
        credential.id,
        credential.phone_ciphertext,
    )


def _active_credential(
    session: Session, *, portal_id: str, lock: bool = False
) -> PatientAccessCredential:
    statement = select(PatientAccessCredential).where(
        PatientAccessCredential.portal_id == portal_id.strip().upper(),
        col(PatientAccessCredential.is_active).is_(True),
        col(PatientAccessCredential.revoked_at).is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    credential = session.exec(statement).first()
    if credential is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    return credential


def _start_challenge(
    session: Session,
    credential: PatientAccessCredential,
    *,
    purpose: str,
) -> StartedOTPChallenge:
    now = get_datetime_utc()
    latest = session.exec(
        select(PatientOTPChallenge)
        .where(
            PatientOTPChallenge.clinic_id == credential.clinic_id,
            PatientOTPChallenge.credential_id == credential.id,
            col(PatientOTPChallenge.consumed_at).is_(None),
            col(PatientOTPChallenge.revoked_at).is_(None),
            PatientOTPChallenge.expires_at > now,
        )
        .order_by(col(PatientOTPChallenge.created_at).desc())
    ).first()
    if latest is not None and latest.resend_available_at > now:
        raise HTTPException(status_code=429, detail="OTP resend is temporarily limited")
    hour_count = session.exec(
        select(func.count())
        .select_from(PatientOTPChallenge)
        .where(
            PatientOTPChallenge.clinic_id == credential.clinic_id,
            PatientOTPChallenge.credential_id == credential.id,
            PatientOTPChallenge.created_at > now - timedelta(hours=1),
        )
    ).one()
    if int(hour_count) >= 5:
        raise HTTPException(status_code=429, detail="OTP request limit reached")
    if latest is not None:
        latest.revoked_at = now
        session.add(latest)
    raw_secret = secrets.token_urlsafe(32)
    challenge_token = f"{credential.clinic_id}.{raw_secret}"
    otp = f"{secrets.randbelow(1_000_000):06d}"
    challenge = PatientOTPChallenge(
        clinic_id=credential.clinic_id,
        credential_id=credential.id,
        purpose=purpose,
        challenge_token_hash=_token_hash(challenge_token),
        otp_hash=security.get_password_hash(otp),
        attempts_remaining=settings.PATIENT_OTP_MAX_ATTEMPTS,
        resend_available_at=now
        + timedelta(seconds=settings.PATIENT_OTP_RESEND_SECONDS),
        expires_at=now + timedelta(seconds=settings.PATIENT_OTP_TTL_SECONDS),
    )
    session.add(challenge)
    session.flush()
    notification, _ = queue_notification(
        session,
        clinic_id=credential.clinic_id,
        patient_id=credential.patient_id,
        purpose="patient_otp",
        channel=credential.preferred_channel,
        destination=_credential_phone(credential),
        template_key="patient-otp-v1",
        payload={"otp": otp, "purpose": purpose},
        idempotency_key=f"patient-otp:{challenge.id}",
    )
    session.flush()
    return StartedOTPChallenge(
        challenge=challenge,
        challenge_token=challenge_token,
        otp=otp,
        masked_phone=credential.masked_phone or "***",
        notification_id=notification.id,
    )


def start_enrollment(
    session: Session,
    *,
    invitation_token: str,
    claim_code: str,
    phone: str,
) -> StartedOTPChallenge:
    """Verify all three enrollment factors before sending the phone OTP.

    The invitation token supplies only the clinic and invitation lookup.  The
    independently delivered, patient-specific claim code binds the invitation
    to one credential, while the supplied phone must match the encrypted phone
    registered by clinic staff.  A shared phone is valid because matching is
    performed against the credential selected by the invitation, never as a
    global identity lookup.
    """

    clinic_text, separator, _secret = invitation_token.partition(".")
    try:
        clinic_id = uuid.UUID(clinic_text) if separator else None
    except ValueError:
        clinic_id = None
    if clinic_id is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    invitation = session.exec(
        select(PatientPortalInvitation)
        .where(
            PatientPortalInvitation.clinic_id == clinic_id,
            PatientPortalInvitation.token_hash == _token_hash(invitation_token),
        )
        .with_for_update()
    ).first()
    if invitation is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    credential = session.exec(
        select(PatientAccessCredential)
        .where(
            PatientAccessCredential.clinic_id == clinic_id,
            PatientAccessCredential.invitation_id == invitation.id,
            col(PatientAccessCredential.is_active).is_(True),
            col(PatientAccessCredential.revoked_at).is_(None),
        )
        .with_for_update()
    ).first()
    now = get_datetime_utc()
    normalized_phone = normalize_phone(phone)
    if (
        credential is None
        or invitation.clinic_id != clinic_id
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
        or credential.claim_code_used_at is not None
        or credential.claim_code_expires_at <= now
        or not hmac_compare(credential.claim_code_hash, _token_hash(claim_code.upper()))
        or credential.phone_hmac
        != field_codec.blind_index(clinic_id, "patient_access.phone", normalized_phone)
    ):
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    return _start_challenge(session, credential, purpose="enrollment")


def start_login(session: Session, *, portal_id: str) -> StartedOTPChallenge:
    credential = _active_credential(session, portal_id=portal_id, lock=True)
    if credential.user_id is None or credential.claim_code_used_at is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    return _start_challenge(session, credential, purpose="login")


def resend_challenge(session: Session, *, challenge_token: str) -> StartedOTPChallenge:
    """Rotate an eligible challenge without extending an undisclosed secret."""

    challenge = session.exec(
        select(PatientOTPChallenge)
        .where(PatientOTPChallenge.challenge_token_hash == _token_hash(challenge_token))
        .with_for_update()
    ).first()
    now = get_datetime_utc()
    if (
        challenge is None
        or challenge.consumed_at is not None
        or challenge.revoked_at is not None
        or challenge.expires_at <= now
        or challenge.attempts_remaining <= 0
    ):
        raise HTTPException(status_code=400, detail="OTP is invalid or expired")
    if challenge.resend_available_at > now:
        raise HTTPException(status_code=429, detail="OTP resend is temporarily limited")
    credential = session.exec(
        select(PatientAccessCredential)
        .where(
            PatientAccessCredential.clinic_id == challenge.clinic_id,
            PatientAccessCredential.id == challenge.credential_id,
            col(PatientAccessCredential.is_active).is_(True),
            col(PatientAccessCredential.revoked_at).is_(None),
        )
        .with_for_update()
    ).first()
    if credential is None:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    challenge.revoked_at = now
    session.add(challenge)
    session.flush()
    return _start_challenge(session, credential, purpose=challenge.purpose)


def hmac_compare(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)


def verify_challenge(
    session: Session, *, challenge_token: str, otp: str
) -> tuple[User, ClinicMembership, PatientAccessCredential]:
    challenge = session.exec(
        select(PatientOTPChallenge)
        .where(PatientOTPChallenge.challenge_token_hash == _token_hash(challenge_token))
        .with_for_update()
    ).first()
    now = get_datetime_utc()
    if (
        challenge is None
        or challenge.consumed_at is not None
        or challenge.revoked_at is not None
        or challenge.expires_at <= now
        or challenge.attempts_remaining <= 0
    ):
        raise HTTPException(status_code=400, detail="OTP is invalid or expired")
    verified, _ = security.verify_password(otp, challenge.otp_hash)
    if not verified:
        challenge.attempts_remaining -= 1
        if challenge.attempts_remaining <= 0:
            challenge.revoked_at = now
        session.add(challenge)
        session.commit()
        raise HTTPException(status_code=400, detail="OTP is invalid or expired")
    credential = session.exec(
        select(PatientAccessCredential)
        .where(
            PatientAccessCredential.clinic_id == challenge.clinic_id,
            PatientAccessCredential.id == challenge.credential_id,
        )
        .with_for_update()
    ).first()
    if (
        credential is None
        or not credential.is_active
        or credential.revoked_at is not None
        or challenge.purpose not in {"enrollment", "login"}
    ):
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    patient = session.get(Patient, credential.patient_id)
    if patient is None or patient.clinic_id != credential.clinic_id:
        raise HTTPException(status_code=400, detail="Patient access is invalid")
    if credential.user_id is not None:
        set_rls_actor(
            session,
            credential.user_id,
            role="patient",
            patient_id=credential.patient_id,
        )
    user = session.get(User, credential.user_id) if credential.user_id else None
    if challenge.purpose == "enrollment":
        if user is not None or credential.claim_code_used_at is not None:
            raise HTTPException(
                status_code=409, detail="Patient access already claimed"
            )
        invitation = (
            session.get(PatientPortalInvitation, credential.invitation_id)
            if credential.invitation_id is not None
            else None
        )
        if invitation is None:
            raise HTTPException(status_code=400, detail="Patient access is invalid")
        user_id = uuid.uuid4()
        set_rls_actor(
            session,
            user_id,
            role="patient",
            patient_id=credential.patient_id,
        )
        user = User(
            id=user_id,
            account_kind="patient",
            email=None,
            hashed_password=None,
        )
        session.add(user)
        session.flush()
        membership = ClinicMembership(
            clinic_id=credential.clinic_id,
            user_id=user.id,
            role="patient",
        )
        session.add(membership)
        session.flush()
        link = PatientUserLink(
            clinic_id=credential.clinic_id,
            patient_id=credential.patient_id,
            user_id=user.id,
        )
        session.add(link)
        # Strict RLS recognizes the new patient actor only after its exact
        # self-membership and patient link exist in this transaction.
        session.flush()
        credential.user_id = user.id
        credential.claim_code_used_at = now
        invitation.accepted_at = now
        session.add(invitation)
    else:
        if user is None or not user.is_active:
            raise HTTPException(status_code=400, detail="Patient access is invalid")
        existing_membership = session.exec(
            select(ClinicMembership).where(
                ClinicMembership.clinic_id == credential.clinic_id,
                ClinicMembership.user_id == user.id,
                ClinicMembership.role == "patient",
                col(ClinicMembership.is_active).is_(True),
            )
        ).first()
        if existing_membership is None:
            raise HTTPException(status_code=400, detail="Patient access is invalid")
        membership = existing_membership
    challenge.consumed_at = now
    credential.updated_at = now
    session.add(challenge)
    session.add(credential)
    session.add(
        AuditEvent(
            clinic_id=credential.clinic_id,
            actor_id=user.id,
            action=(
                "patient.phone_access_enrolled"
                if challenge.purpose == "enrollment"
                else "patient.phone_access_authenticated"
            ),
            resource_type="patient",
            resource_id=credential.patient_id,
            reason_code="verified_phone_otp",
            metadata_json={"portal_access": True},
        )
    )
    session.flush()
    return user, membership, credential


def dispatch_queued_access_notification(
    session: Session, *, clinic_id: uuid.UUID, notification_id: uuid.UUID
) -> NotificationOutbox:
    if not bind_notification_worker(session, clinic_id):
        raise HTTPException(status_code=503, detail="Delivery worker unavailable")
    notification = session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.clinic_id == clinic_id,
            NotificationOutbox.id == notification_id,
        )
    ).one()
    return dispatch_notification(session, notification)
