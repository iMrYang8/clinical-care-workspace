from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    ClinicFormularyQualificationRequest,
    ClinicFormularyReadinessPublic,
    ClinicFormularyVersion,
    ClinicFormularyVersionCreate,
    ClinicFormularyVersionPublic,
    ClinicFormularyVersionsPublic,
)
from app.services.clinical_formulary import (
    FormularyConfigurationError,
    activate_clinic_formulary_version,
    clinic_formulary_readiness,
    create_clinic_formulary_draft,
    formulary_version_public,
    qualify_clinic_formulary_version,
)
from app.services.nightingale import emit_change

router = APIRouter(prefix="/admin/formulary", tags=["admin-formulary"])


def _require_admin(context: CurrentContext) -> None:
    if context.role != "admin":
        raise HTTPException(status_code=403, detail="Clinic admin role required")


def _configuration_error(error: FormularyConfigurationError) -> HTTPException:
    invalid = {
        "FORMULARY_CONCEPTS_REQUIRED",
        "FORMULARY_CONCEPT_CODE_INVALID",
        "FORMULARY_CONCEPT_CODE_DUPLICATE",
        "FORMULARY_CANONICAL_NAME_INVALID",
        "FORMULARY_MULTILINGUAL_ALIASES_INCOMPLETE",
        "FORMULARY_ALIAS_INVALID",
        "FORMULARY_ALIAS_DUPLICATE",
        "FORMULARY_ALIAS_AMBIGUOUS",
        "FORMULARY_DOSE_UNIT_UNKNOWN",
        "FORMULARY_DOSE_RANGE_INVALID",
        "FORMULARY_ROUTE_UNKNOWN",
        "FORMULARY_ALLERGY_CONCEPT_INVALID",
        "FORMULARY_ALLERGY_CONCEPT_DUPLICATE",
        "FORMULARY_VERSION_CODE_INVALID",
        "FORMULARY_EXPECTED_DIGEST_INVALID",
    }
    return HTTPException(
        status_code=422 if error.code in invalid else 409,
        detail={"code": error.code, "review_required": True},
    )


def _version(
    session: SessionDep,
    context: CurrentContext,
    version_id: uuid.UUID,
) -> ClinicFormularyVersion:
    row = session.exec(
        select(ClinicFormularyVersion).where(
            ClinicFormularyVersion.clinic_id == context.clinic_id,
            ClinicFormularyVersion.id == version_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Formulary version not found")
    return row


@router.get("/versions", response_model=ClinicFormularyVersionsPublic)
def list_formulary_versions(
    session: SessionDep,
    context: CurrentContext,
) -> ClinicFormularyVersionsPublic:
    _require_admin(context)
    rows = session.exec(
        select(ClinicFormularyVersion)
        .where(ClinicFormularyVersion.clinic_id == context.clinic_id)
        .order_by(col(ClinicFormularyVersion.created_at).desc())
    ).all()
    data = [
        formulary_version_public(session, row, include_concepts=False) for row in rows
    ]
    return ClinicFormularyVersionsPublic(data=data, count=len(data))


@router.get("/versions/{version_id}", response_model=ClinicFormularyVersionPublic)
def read_formulary_version(
    version_id: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> ClinicFormularyVersionPublic:
    _require_admin(context)
    return formulary_version_public(session, _version(session, context, version_id))


@router.get("/readiness", response_model=ClinicFormularyReadinessPublic)
def read_formulary_readiness(
    session: SessionDep,
    context: CurrentContext,
) -> ClinicFormularyReadinessPublic:
    _require_admin(context)
    return clinic_formulary_readiness(session, context.clinic_id)


@router.post(
    "/versions",
    response_model=ClinicFormularyVersionPublic,
    status_code=201,
)
def create_formulary_version(
    body: ClinicFormularyVersionCreate,
    session: SessionDep,
    context: CurrentContext,
) -> ClinicFormularyVersionPublic:
    _require_admin(context)
    try:
        version = create_clinic_formulary_draft(
            session,
            clinic_id=context.clinic_id,
            membership_id=context.membership.id,
            body=body,
        )
    except FormularyConfigurationError as error:
        raise _configuration_error(error) from error
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "FORMULARY_VERSION_ALREADY_EXISTS",
                "review_required": True,
            },
        ) from error
    emit_change(
        session,
        context,
        action="clinic.formulary.version_created",
        resource_type="clinic_formulary_version",
        resource_id=version.id,
        metadata={
            "version_id": str(version.id),
            "candidate_count": len(body.concepts),
            "status": version.status,
        },
        reason_code="formulary_version_created",
    )
    session.commit()
    session.refresh(version)
    return formulary_version_public(session, version)


@router.post(
    "/versions/{version_id}/qualify",
    response_model=ClinicFormularyVersionPublic,
)
def qualify_formulary_version(
    version_id: uuid.UUID,
    body: ClinicFormularyQualificationRequest,
    session: SessionDep,
    context: CurrentContext,
) -> ClinicFormularyVersionPublic:
    _require_admin(context)
    try:
        version = qualify_clinic_formulary_version(
            session,
            clinic_id=context.clinic_id,
            membership_id=context.membership.id,
            version_id=version_id,
            expected_content_sha256=body.expected_content_sha256,
        )
    except FormularyConfigurationError as error:
        raise _configuration_error(error) from error
    if version is None:
        raise HTTPException(status_code=404, detail="Formulary version not found")
    emit_change(
        session,
        context,
        action="clinic.formulary.version_qualified",
        resource_type="clinic_formulary_version",
        resource_id=version.id,
        metadata={"version_id": str(version.id), "status": version.status},
        reason_code="formulary_digest_qualified",
    )
    session.commit()
    session.refresh(version)
    return formulary_version_public(session, version)


@router.post(
    "/versions/{version_id}/activate",
    response_model=ClinicFormularyVersionPublic,
)
def activate_formulary_version(
    version_id: uuid.UUID,
    body: ClinicFormularyQualificationRequest,
    session: SessionDep,
    context: CurrentContext,
) -> ClinicFormularyVersionPublic:
    _require_admin(context)
    try:
        version, previous_version_id = activate_clinic_formulary_version(
            session,
            clinic_id=context.clinic_id,
            membership_id=context.membership.id,
            version_id=version_id,
            expected_content_sha256=body.expected_content_sha256,
        )
    except FormularyConfigurationError as error:
        raise _configuration_error(error) from error
    if version is None:
        raise HTTPException(status_code=404, detail="Formulary version not found")
    emit_change(
        session,
        context,
        action="clinic.formulary.version_activated",
        resource_type="clinic_formulary_version",
        resource_id=version.id,
        metadata={
            "version_id": str(version.id),
            "previous_version_id": (
                str(previous_version_id) if previous_version_id is not None else None
            ),
            "status": version.status,
        },
        reason_code="formulary_version_activated",
    )
    session.commit()
    session.refresh(version)
    return formulary_version_public(session, version)
