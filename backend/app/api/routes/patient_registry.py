from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import jwt
from fastapi import APIRouter, Header, HTTPException, Response
from jwt.exceptions import InvalidTokenError
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.core import security
from app.core.config import settings
from app.core.db import set_rls_clinic
from app.core.field_crypto import field_codec
from app.models import (
    AuditEvent,
    Clinic,
    ClinicMembership,
    Patient,
    PatientCreate,
    PatientDetailPublic,
    PatientDuplicateCandidate,
    PatientDuplicateCheckPublic,
    PatientGlanceSnapshot,
    PatientIdentifier,
    PatientIdentityInput,
    PatientInvitationAccept,
    PatientInvitationPreviewPublic,
    PatientInvitationPreviewRequest,
    PatientPortalInvitation,
    PatientPortalInvitationCreate,
    PatientPortalInvitationPublic,
    PatientUserLink,
    Token,
    User,
    get_datetime_utc,
)
from app.services.invitations import deliver_patient_portal_invitation
from app.services.nightingale import emit_change, get_patient

router = APIRouter(prefix="/patients", tags=["patients"])
auth_router = APIRouter(prefix="/auth/patient-invitations", tags=["auth"])

_IDENTIFIER = re.compile(r"[^A-Z0-9]")


def _require_registrar(context: CurrentContext) -> None:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Patient registrar role required")


def _normalize_name(value: str) -> str:
    return " ".join(value.strip().split())


def _normalize_identifier(value: str) -> str:
    normalized = _IDENTIFIER.sub("", value.strip().upper())
    if len(normalized) < 3:
        raise HTTPException(status_code=422, detail="Patient identifier is invalid")
    return normalized


def _canonical(body: PatientIdentityInput) -> dict[str, str]:
    return {
        "display_name": _normalize_name(body.display_name),
        "date_of_birth": body.date_of_birth.isoformat(),
        "medical_record_number": _normalize_identifier(body.medical_record_number),
        "identity_document_type": body.identity_document_type,
        "identity_document_number": _normalize_identifier(
            body.identity_document_number
        ),
    }


def _payload_digest(canonical: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _confirmation_token(clinic_id: uuid.UUID, digest: str) -> str:
    return jwt.encode(
        {
            "scope": "patient-duplicate-confirmation",
            "clinic_id": str(clinic_id),
            "identity_digest": digest,
            "exp": datetime.now(UTC) + timedelta(minutes=10),
        },
        settings.SECRET_KEY,
        algorithm=security.ALGORITHM,
    )


def _valid_confirmation(token: str | None, clinic_id: uuid.UUID, digest: str) -> bool:
    if not token:
        return False
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
    except InvalidTokenError:
        return False
    return (
        payload.get("scope") == "patient-duplicate-confirmation"
        and payload.get("clinic_id") == str(clinic_id)
        and payload.get("identity_digest") == digest
    )


def _decrypt_identifier(item: PatientIdentifier) -> str:
    return field_codec.decrypt_text(
        item.clinic_id,
        "patient_identifier.value",
        item.id,
        item.value_ciphertext,
    )


def _identifiers_for(
    session: SessionDep, clinic_id: uuid.UUID, patient_id: uuid.UUID
) -> list[PatientIdentifier]:
    return list(
        session.exec(
            select(PatientIdentifier).where(
                PatientIdentifier.clinic_id == clinic_id,
                PatientIdentifier.patient_id == patient_id,
            )
        ).all()
    )


def _candidate(session: SessionDep, patient: Patient) -> PatientDuplicateCandidate:
    identifiers = _identifiers_for(session, patient.clinic_id, patient.id)
    mrn = next(
        (
            _decrypt_identifier(item)
            for item in identifiers
            if item.identifier_type == "medical_record_number"
        ),
        None,
    )
    identity = next(
        (
            item
            for item in identifiers
            if item.identifier_type != "medical_record_number"
        ),
        None,
    )
    dob = None
    if patient.date_of_birth_ciphertext:
        dob = datetime.strptime(
            field_codec.decrypt_text(
                patient.clinic_id,
                "patient.date_of_birth",
                patient.id,
                patient.date_of_birth_ciphertext,
            ),
            "%Y-%m-%d",
        ).date()
    return PatientDuplicateCandidate(
        patient_id=patient.id,
        display_name=field_codec.decrypt_text(
            patient.clinic_id,
            "patient.display_name",
            patient.id,
            patient.display_name_ciphertext,
        ),
        date_of_birth=dob,
        medical_record_number=mrn,
        masked_identity_document=(
            f"••••{identity.masked_suffix}" if identity is not None else None
        ),
    )


def _duplicate_result(
    session: SessionDep,
    clinic_id: uuid.UUID,
    body: PatientIdentityInput,
) -> PatientDuplicateCheckPublic:
    canonical = _canonical(body)
    mrn_hmac = field_codec.blind_index(
        clinic_id,
        "patient_identifier:medical_record_number",
        canonical["medical_record_number"],
    )
    document_hmac = field_codec.blind_index(
        clinic_id,
        f"patient_identifier:{canonical['identity_document_type']}",
        canonical["identity_document_number"],
    )
    exact_ids = session.exec(
        select(PatientIdentifier).where(
            PatientIdentifier.clinic_id == clinic_id,
            col(PatientIdentifier.value_hmac).in_([mrn_hmac, document_hmac]),
        )
    ).all()
    if exact_ids:
        patients = {
            item.patient_id: session.get(Patient, item.patient_id) for item in exact_ids
        }
        return PatientDuplicateCheckPublic(
            status="exact_match",
            candidates=[
                _candidate(session, patient)
                for patient in patients.values()
                if patient is not None and patient.clinic_id == clinic_id
            ],
        )
    identity_match_hash = field_codec.blind_index(
        clinic_id,
        "patient_identity:name_dob",
        f"{canonical['display_name'].casefold()}|{canonical['date_of_birth']}",
    )
    possible = session.exec(
        select(Patient).where(
            Patient.clinic_id == clinic_id,
            Patient.identity_match_hash == identity_match_hash,
            Patient.status == "active",
        )
    ).all()
    if possible:
        return PatientDuplicateCheckPublic(
            status="possible_match",
            candidates=[_candidate(session, item) for item in possible],
            duplicate_confirmation_token=_confirmation_token(
                clinic_id, _payload_digest(canonical)
            ),
        )
    return PatientDuplicateCheckPublic(status="clear", candidates=[])


def _portal_state(session: SessionDep, patient: Patient) -> str:
    link = session.exec(
        select(PatientUserLink).where(
            PatientUserLink.clinic_id == patient.clinic_id,
            PatientUserLink.patient_id == patient.id,
        )
    ).first()
    if link is not None:
        membership = session.exec(
            select(ClinicMembership).where(
                ClinicMembership.clinic_id == patient.clinic_id,
                ClinicMembership.user_id == link.user_id,
            )
        ).first()
        return "active" if membership and membership.is_active else "deactivated"
    now = get_datetime_utc()
    pending = session.exec(
        select(PatientPortalInvitation).where(
            PatientPortalInvitation.clinic_id == patient.clinic_id,
            PatientPortalInvitation.patient_id == patient.id,
            col(PatientPortalInvitation.accepted_at).is_(None),
            col(PatientPortalInvitation.revoked_at).is_(None),
            PatientPortalInvitation.expires_at > now,
        )
    ).first()
    return "pending" if pending else "not_invited"


def patient_detail(session: SessionDep, patient: Patient) -> PatientDetailPublic:
    candidate = _candidate(session, patient)
    identity = next(
        (
            item
            for item in _identifiers_for(session, patient.clinic_id, patient.id)
            if item.identifier_type != "medical_record_number"
        ),
        None,
    )
    return PatientDetailPublic(
        id=patient.id,
        display_name=candidate.display_name,
        date_of_birth=candidate.date_of_birth,
        medical_record_number=candidate.medical_record_number,
        identity_document_type=identity.identifier_type if identity else None,
        masked_identity_document=candidate.masked_identity_document,
        portal_access_state=cast(
            Literal["not_invited", "pending", "active", "deactivated"],
            _portal_state(session, patient),
        ),
        status=patient.status,
    )


@router.post("/duplicate-check", response_model=PatientDuplicateCheckPublic)
def duplicate_check(
    body: PatientIdentityInput, session: SessionDep, context: CurrentContext
) -> PatientDuplicateCheckPublic:
    _require_registrar(context)
    return _duplicate_result(session, context.clinic_id, body)


@router.post("", response_model=PatientDetailPublic, status_code=201)
def create_patient(
    body: PatientCreate,
    session: SessionDep,
    context: CurrentContext,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PatientDetailPublic:
    _require_registrar(context)
    canonical = _canonical(body)
    patient_id = (
        uuid.uuid5(
            context.clinic_id,
            f"patient-create:{hashlib.sha256(idempotency_key.encode()).hexdigest()}",
        )
        if idempotency_key
        else uuid.uuid4()
    )
    existing = session.get(Patient, patient_id)
    if existing is not None:
        existing_identifiers = {
            item.identifier_type: _decrypt_identifier(item)
            for item in _identifiers_for(session, context.clinic_id, existing.id)
        }
        same_request = (
            existing.identity_match_hash
            == field_codec.blind_index(
                context.clinic_id,
                "patient_identity:name_dob",
                f"{canonical['display_name'].casefold()}|{canonical['date_of_birth']}",
            )
            and existing_identifiers.get("medical_record_number")
            == canonical["medical_record_number"]
            and existing_identifiers.get(canonical["identity_document_type"])
            == canonical["identity_document_number"]
        )
        if not same_request:
            raise HTTPException(
                status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED"}
            )
        return patient_detail(session, existing)
    result = _duplicate_result(session, context.clinic_id, body)
    if result.status == "exact_match":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PATIENT_IDENTITY_EXISTS",
                "candidates": [
                    item.model_dump(mode="json") for item in result.candidates
                ],
            },
        )
    digest = _payload_digest(canonical)
    if result.status == "possible_match" and not _valid_confirmation(
        body.duplicate_confirmation_token, context.clinic_id, digest
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PATIENT_DUPLICATE_CONFIRMATION_REQUIRED",
                "candidates": [
                    item.model_dump(mode="json") for item in result.candidates
                ],
                "duplicate_confirmation_token": result.duplicate_confirmation_token,
            },
        )
    patient = Patient(
        id=patient_id,
        clinic_id=context.clinic_id,
        display_name_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "patient.display_name",
            patient_id,
            canonical["display_name"],
        ),
        date_of_birth_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "patient.date_of_birth",
            patient_id,
            canonical["date_of_birth"],
        ),
        external_ref_hash=field_codec.blind_index(
            context.clinic_id,
            "patient_identifier:medical_record_number",
            canonical["medical_record_number"],
        ),
        identity_match_hash=field_codec.blind_index(
            context.clinic_id,
            "patient_identity:name_dob",
            f"{canonical['display_name'].casefold()}|{canonical['date_of_birth']}",
        ),
        created_by_membership_id=context.membership.id,
    )
    session.add(patient)
    session.flush()
    for identifier_type, value in (
        ("medical_record_number", canonical["medical_record_number"]),
        (canonical["identity_document_type"], canonical["identity_document_number"]),
    ):
        identifier_id = uuid.uuid4()
        session.add(
            PatientIdentifier(
                id=identifier_id,
                clinic_id=context.clinic_id,
                patient_id=patient.id,
                identifier_type=identifier_type,
                value_ciphertext=field_codec.encrypt_text(
                    context.clinic_id, "patient_identifier.value", identifier_id, value
                ),
                value_hmac=field_codec.blind_index(
                    context.clinic_id, f"patient_identifier:{identifier_type}", value
                ),
                masked_suffix=value[-4:],
                created_by_membership_id=context.membership.id,
            )
        )
    snapshot_id = uuid.uuid4()
    session.add(
        PatientGlanceSnapshot(
            id=snapshot_id,
            clinic_id=context.clinic_id,
            patient_id=patient.id,
            payload_ciphertext=field_codec.encrypt_json(
                context.clinic_id,
                "glance.payload",
                snapshot_id,
                {"cards": [], "patient_cards": []},
            ),
        )
    )
    emit_change(
        session,
        context,
        action="patient.created",
        resource_type="patient",
        resource_id=patient.id,
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        set_rls_clinic(session, context.clinic_id)
        replay = session.get(Patient, patient_id) if idempotency_key else None
        if replay is not None:
            return patient_detail(session, replay)
        raise HTTPException(
            status_code=409,
            detail={"code": "PATIENT_IDENTITY_EXISTS"},
        )
    session.refresh(patient)
    return patient_detail(session, patient)


@router.get("/{patient_id}", response_model=PatientDetailPublic)
def read_patient(
    patient_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> PatientDetailPublic:
    patient = get_patient(session, context, patient_id)
    return patient_detail(session, patient)


@router.post(
    "/{patient_id}/portal-invitations",
    response_model=PatientPortalInvitationPublic,
    status_code=201,
)
def invite_patient(
    patient_id: uuid.UUID,
    body: PatientPortalInvitationCreate,
    session: SessionDep,
    context: CurrentContext,
) -> PatientPortalInvitationPublic:
    _require_registrar(context)
    patient = get_patient(session, context, patient_id)
    if _portal_state(session, patient) == "active":
        raise HTTPException(
            status_code=409, detail="Patient portal access already active"
        )
    normalized_email = str(body.email).strip().lower()
    now = get_datetime_utc()
    pending = session.exec(
        select(PatientPortalInvitation).where(
            PatientPortalInvitation.clinic_id == context.clinic_id,
            PatientPortalInvitation.patient_id == patient.id,
            col(PatientPortalInvitation.accepted_at).is_(None),
            col(PatientPortalInvitation.revoked_at).is_(None),
            PatientPortalInvitation.expires_at > now,
        )
    ).first()
    if pending is not None:
        raise HTTPException(
            status_code=409, detail="Active patient invitation already exists"
        )
    secret = secrets.token_urlsafe(32)
    raw_token = f"{context.clinic_id}.{secret}"
    invitation = PatientPortalInvitation(
        clinic_id=context.clinic_id,
        patient_id=patient.id,
        email=normalized_email,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        created_by_membership_id=context.membership.id,
        expires_at=now + timedelta(hours=24),
    )
    session.add(invitation)
    session.flush()
    emit_change(
        session,
        context,
        action="patient.portal_invited",
        resource_type="patient_portal_invitation",
        resource_id=invitation.id,
    )
    session.commit()
    clinic = session.get(Clinic, context.clinic_id)
    try:
        deliver_patient_portal_invitation(
            recipient=normalized_email,
            token=raw_token,
            clinic_name=clinic.name if clinic else "Your clinic",
        )
    except Exception:
        invitation.revoked_at = get_datetime_utc()
        session.add(invitation)
        session.commit()
        raise HTTPException(
            status_code=503, detail="Invitation delivery did not complete"
        )
    return PatientPortalInvitationPublic.model_validate(invitation)


def _invitation(
    session: SessionDep,
    body: PatientInvitationPreviewRequest | PatientInvitationAccept,
    *,
    lock: bool,
) -> tuple[Clinic, PatientPortalInvitation, Patient]:
    clinic_text, separator, _ = body.token.partition(".")
    try:
        clinic_id = uuid.UUID(clinic_text) if separator else None
    except ValueError:
        clinic_id = None
    if clinic_id is None:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    set_rls_clinic(session, clinic_id)
    statement = select(PatientPortalInvitation).where(
        PatientPortalInvitation.clinic_id == clinic_id,
        PatientPortalInvitation.token_hash
        == hashlib.sha256(body.token.encode()).hexdigest(),
    )
    if lock:
        statement = statement.with_for_update()
    invitation = session.exec(statement).first()
    now = get_datetime_utc()
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
        or str(invitation.email).strip().lower() != str(body.email).strip().lower()
    ):
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    clinic = session.get(Clinic, clinic_id)
    patient = session.get(Patient, invitation.patient_id)
    creator = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.id == invitation.created_by_membership_id,
            ClinicMembership.clinic_id == clinic_id,
            col(ClinicMembership.is_active).is_(True),
            col(ClinicMembership.role).in_(["staff", "clinician"]),
        )
    ).first()
    if (
        clinic is None
        or patient is None
        or patient.clinic_id != clinic_id
        or creator is None
    ):
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    return clinic, invitation, patient


@auth_router.post("/preview", response_model=PatientInvitationPreviewPublic)
def preview_patient_invitation(
    body: PatientInvitationPreviewRequest, session: SessionDep
) -> PatientInvitationPreviewPublic:
    clinic, invitation, patient = _invitation(session, body, lock=False)
    user = session.exec(
        select(User).where(User.email == str(body.email).strip().lower())
    ).first()
    return PatientInvitationPreviewPublic(
        clinic_name=clinic.name,
        patient_display_name=field_codec.decrypt_text(
            clinic.id,
            "patient.display_name",
            patient.id,
            patient.display_name_ciphertext,
        ),
        email=invitation.email,
        account_exists=user is not None,
    )


@auth_router.post("/accept", response_model=Token)
def accept_patient_invitation(
    body: PatientInvitationAccept,
    response: Response,
    session: SessionDep,
) -> Token:
    clinic, invitation, patient = _invitation(session, body, lock=True)
    normalized_email = str(body.email).strip().lower()
    user = session.exec(select(User).where(User.email == normalized_email)).first()
    if user is None:
        if len(body.password) < 16:
            raise HTTPException(
                status_code=422,
                detail="New patient portal passwords must be at least 16 characters",
            )
        user = User(
            email=normalized_email,
            full_name=body.full_name,
            hashed_password=security.get_password_hash(body.password),
        )
        session.add(user)
        session.flush()
    else:
        valid_password, updated_hash = security.verify_password(
            body.password, user.hashed_password
        )
        if not valid_password or not user.is_active:
            raise HTTPException(
                status_code=400, detail="Invitation is invalid or expired"
            )
        if updated_hash:
            user.hashed_password = updated_hash
            session.add(user)
    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == clinic.id,
            ClinicMembership.user_id == user.id,
        )
    ).first()
    if membership is None:
        membership = ClinicMembership(
            clinic_id=clinic.id, user_id=user.id, role="patient"
        )
        session.add(membership)
        session.flush()
    elif membership.role != "patient" or not membership.is_active:
        raise HTTPException(
            status_code=409, detail="Account cannot accept patient access"
        )
    link = session.exec(
        select(PatientUserLink).where(
            PatientUserLink.clinic_id == clinic.id,
            PatientUserLink.patient_id == patient.id,
            PatientUserLink.user_id == user.id,
        )
    ).first()
    if link is None:
        session.add(
            PatientUserLink(clinic_id=clinic.id, patient_id=patient.id, user_id=user.id)
        )
    invitation.accepted_at = get_datetime_utc()
    session.add(invitation)
    session.add(
        AuditEvent(
            clinic_id=clinic.id,
            actor_id=user.id,
            action="patient.portal_invitation_accepted",
            resource_type="patient",
            resource_id=patient.id,
            metadata_json={},
        )
    )
    session.commit()
    token = Token(
        access_token=security.create_access_token(
            user.id,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            membership_id=membership.id,
            clinic_id=clinic.id,
        )
    )
    response.set_cookie(
        key=settings.AUTH_COOKIE_NAME,
        value=token.access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )
    return token
