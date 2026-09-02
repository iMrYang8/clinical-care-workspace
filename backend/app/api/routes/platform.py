from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jwt
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlalchemy import func, text
from sqlmodel import Session, col, select

from app.api.deps import RequestContext, SessionDep
from app.api.routes.patient_registry import patient_detail
from app.core import security
from app.core.config import (
    ExternalObservabilityRetentionEvidence,
    external_observability_retention_capability,
    settings,
)
from app.core.db import set_rls_actor, set_rls_clinic
from app.models import (
    Clinic,
    ClinicChannelCapabilityEvidencePublic,
    ClinicInvitation,
    ClinicMembership,
    ClinicOnboardingCreate,
    ClinicOperationalSetting,
    ClinicOperationalSettingPublic,
    ClinicPreflightCheckPublic,
    ClinicPreflightEvidencePublic,
    ClinicPreflightPublic,
    NotificationOutbox,
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
    WorkerHeartbeat,
    get_datetime_utc,
)
from app.services.clinical_formulary import (
    FormularyConfigurationError,
    seed_clinic_formulary_template,
    validate_formulary_template,
)
from app.services.messaging import (
    NotificationChannelUnavailable,
    bind_notification_worker,
    dispatch_notification,
    notification_channel_capabilities,
    queue_notification,
)
from app.services.nightingale import timeline
from app.services.worker_heartbeat import AI_WORKER_KIND, ai_worker_capability

router = APIRouter(prefix="/platform", tags=["platform"])
_CLINIC_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,78}[a-z0-9])$")


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
    set_rls_actor(session, user_id, role="platform_admin")
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


def _correlation_id(value: str | None) -> str:
    """Accept only an opaque UUID; discard free-form header text."""

    if value is not None:
        try:
            return str(uuid.UUID(value.strip()))
        except ValueError:
            pass
    return str(uuid.uuid4())


def _audit(
    session: Session,
    context: PlatformContext,
    *,
    action: str,
    request_id: str | None,
    clinic_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    reason_code: str = "not_specified",
) -> None:
    session.add(
        PlatformAuditEvent(
            platform_admin_id=context.administrator.id,
            action=action,
            target_clinic_id=clinic_id,
            target_patient_id=patient_id,
            request_id=_correlation_id(request_id),
            reason_code=reason_code,
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


def _setting_public(row: ClinicOperationalSetting) -> ClinicOperationalSettingPublic:
    return ClinicOperationalSettingPublic(
        clinic_id=row.clinic_id,
        timezone=row.timezone,
        worker_enabled=row.worker_enabled,
        supported_languages=row.supported_languages_json,
        messaging_channels=row.messaging_channels_json,
        remote_text_egress_enabled=row.remote_text_egress_enabled,
        remote_audio_egress_enabled=row.remote_audio_egress_enabled,
        calibration_required=row.calibration_required,
        external_proxy_retention_days=row.external_proxy_retention_days,
        external_container_retention_days=row.external_container_retention_days,
        external_apm_retention_days=row.external_apm_retention_days,
        external_observability_retention_evidence=cast(
            ExternalObservabilityRetentionEvidence,
            row.external_observability_retention_evidence,
        ),
        external_observability_retention_evidence_id=(
            row.external_observability_retention_evidence_id
        ),
        formulary_template=row.formulary_template,
        onboarding_status=cast(
            Literal["draft", "ready", "blocked"], row.onboarding_status
        ),
        updated_at=row.updated_at,
    )


def _preflight(session: Session, body: ClinicOnboardingCreate) -> ClinicPreflightPublic:
    observed_at = get_datetime_utc()
    code = body.code.strip().upper()
    slug = body.slug.strip().lower()
    existing_code = session.exec(select(Clinic).where(Clinic.code == code)).first()
    existing_slug = session.exec(select(Clinic).where(Clinic.slug == slug)).first()
    code_ok = bool(re.fullmatch(r"[A-Z]{3,12}", code)) and existing_code is None
    slug_ok = bool(_CLINIC_SLUG.fullmatch(slug)) and existing_slug is None
    try:
        ZoneInfo(body.timezone)
        timezone_ok = True
    except (ZoneInfoNotFoundError, ValueError):
        timezone_ok = False
    staff_emails = [str(item.email).strip().lower() for item in body.initial_staff]
    staff_ok = bool(body.initial_staff) and len(staff_emails) == len(set(staff_emails))
    staff_ok = staff_ok and any(item.role == "admin" for item in body.initial_staff)
    languages = set(body.supported_languages)
    available_languages = {"en", "ms", "nan", "zh", "cmn"}
    languages_ok = {"en", "ms", "nan"}.issubset(languages) and languages.issubset(
        available_languages
    )
    messaging = set(body.messaging_channels)
    channel_capabilities = notification_channel_capabilities()
    messaging_capabilities_ok = all(
        channel_capabilities.get(channel) is not None
        and channel_capabilities[channel].configured
        and (
            settings.FASTAPI_ENV == "development"
            or channel_capabilities[channel].production_safe
        )
        for channel in messaging
    )
    messaging_ok = (
        bool(messaging) and "email" in messaging and messaging_capabilities_ok
    )
    deployment_worker_ok, worker_reason = ai_worker_capability(session)
    worker_heartbeat = session.exec(
        select(WorkerHeartbeat).where(WorkerHeartbeat.worker_kind == AI_WORKER_KIND)
    ).first()
    worker_ok = body.worker_enabled and deployment_worker_ok
    remote_text_ready = not body.remote_text_egress_enabled or (
        settings.AI_PROVIDER == "openai"
        and settings.REMOTE_TEXT_EGRESS_ENABLED
        and settings.PRESIDIO_REQUIRED
        and bool(settings.OPENAI_API_KEY)
        and bool(settings.OPENAI_EXTRACT_MODEL)
    )
    remote_audio_ready = not body.remote_audio_egress_enabled or (
        settings.REMOTE_AUDIO_EGRESS_ENABLED
        and not settings.STRICT_NO_AUDIO_EGRESS
        and settings.PRESIDIO_REQUIRED
        and bool(settings.OPENAI_API_KEY)
        and bool(settings.OPENAI_TRANSCRIBE_MODEL)
        and (
            settings.VOICE_TRANSCRIPTION_PROVIDER == "openai"
            or settings.LIVE_TRANSCRIPT_PROVIDER == "openai"
        )
    )
    egress_ok = remote_text_ready and remote_audio_ready
    external_retention = external_observability_retention_capability(settings)
    try:
        validate_formulary_template(body.formulary_template)
        formulary_ok = True
    except FormularyConfigurationError:
        formulary_ok = False
    checks = [
        ClinicPreflightCheckPublic(
            key="code",
            passed=code_ok and slug_ok,
            reason_code=None if code_ok and slug_ok else "code_or_slug_unavailable",
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=(
                    "request:clinic-identity:"
                    + hashlib.sha256(f"{code}|{slug}".encode()).hexdigest()[:20]
                ),
                observed_at=observed_at,
                source="request",
                requested_code=code,
                requested_slug=slug,
            ),
        ),
        ClinicPreflightCheckPublic(
            key="timezone",
            passed=timezone_ok,
            reason_code=None if timezone_ok else "timezone_unknown",
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=f"runtime:tzdb:{body.timezone}",
                observed_at=observed_at,
                source="runtime",
                timezone=body.timezone,
            ),
        ),
        ClinicPreflightCheckPublic(
            key="initial_staff",
            passed=staff_ok,
            reason_code=None if staff_ok else "active_admin_invitation_required",
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=(
                    f"request:initial-staff:{len(body.initial_staff)}:"
                    f"{sum(item.role == 'admin' for item in body.initial_staff)}"
                ),
                observed_at=observed_at,
                source="request",
                initial_staff_count=len(body.initial_staff),
                initial_admin_count=sum(
                    item.role == "admin" for item in body.initial_staff
                ),
            ),
        ),
        ClinicPreflightCheckPublic(
            key="worker",
            passed=worker_ok,
            reason_code=(
                None
                if worker_ok
                else (
                    worker_reason if body.worker_enabled else "clinic_worker_disabled"
                )
            ),
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=(
                    f"runtime:worker:{worker_heartbeat.id}"
                    if worker_heartbeat is not None
                    else "runtime:worker:missing"
                ),
                observed_at=observed_at,
                source="runtime",
                worker_enabled=body.worker_enabled,
                worker_kind=AI_WORKER_KIND,
                worker_version=(
                    worker_heartbeat.worker_version
                    if worker_heartbeat is not None
                    else None
                ),
                worker_source_commit=(
                    worker_heartbeat.source_commit
                    if worker_heartbeat is not None
                    else None
                ),
                worker_heartbeat_at=(
                    worker_heartbeat.updated_at
                    if worker_heartbeat is not None
                    else None
                ),
                worker_heartbeat_age_seconds=(
                    max(
                        0,
                        int(
                            (observed_at - worker_heartbeat.updated_at).total_seconds()
                        ),
                    )
                    if worker_heartbeat is not None
                    else None
                ),
                worker_heartbeat_max_age_seconds=(
                    settings.AI_WORKER_HEARTBEAT_MAX_AGE_SECONDS
                ),
            ),
        ),
        ClinicPreflightCheckPublic(
            key="languages",
            passed=languages_ok,
            reason_code=None if languages_ok else "en_ms_nan_required",
            evidence=ClinicPreflightEvidencePublic(
                evidence_id="runtime:language-capabilities:v1",
                observed_at=observed_at,
                source="runtime",
                requested_languages=sorted(languages),
                available_languages=sorted(available_languages),
                missing_languages=sorted(
                    ({"en", "ms", "nan"} - languages)
                    | (languages - available_languages)
                ),
            ),
        ),
        ClinicPreflightCheckPublic(
            key="messaging",
            passed=messaging_ok,
            reason_code=(
                None
                if messaging_ok
                else (
                    "email_delivery_required"
                    if "email" not in messaging
                    else "messaging_capability_unavailable"
                )
            ),
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=(
                    "deployment:messaging:"
                    + hashlib.sha256(
                        "|".join(
                            f"{channel}:{channel_capabilities[channel].provider}:"
                            f"{channel_capabilities[channel].configured}:"
                            f"{channel_capabilities[channel].production_safe}"
                            for channel in sorted(channel_capabilities)
                        ).encode()
                    ).hexdigest()[:20]
                ),
                observed_at=observed_at,
                source="deployment",
                channels=[
                    ClinicChannelCapabilityEvidencePublic(
                        channel=channel,
                        provider=channel_capabilities[channel].provider,
                        configured=channel_capabilities[channel].configured,
                        production_safe=channel_capabilities[channel].production_safe,
                        reason_code=channel_capabilities[channel].reason_code,
                    )
                    for channel in sorted(messaging)
                    if channel in channel_capabilities
                ],
            ),
        ),
        ClinicPreflightCheckPublic(
            key="egress_policy",
            passed=egress_ok,
            reason_code=(
                None if egress_ok else "requested_egress_not_deployment_ready"
            ),
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=(
                    f"deployment:egress:{body.remote_text_egress_enabled}:"
                    f"{body.remote_audio_egress_enabled}:{remote_text_ready}:"
                    f"{remote_audio_ready}"
                ),
                observed_at=observed_at,
                source="deployment",
                remote_text_requested=body.remote_text_egress_enabled,
                remote_text_deployment_ready=remote_text_ready,
                remote_audio_requested=body.remote_audio_egress_enabled,
                remote_audio_deployment_ready=remote_audio_ready,
                local_asr_default=settings.VOICE_TRANSCRIPTION_PROVIDER != "openai",
            ),
        ),
        ClinicPreflightCheckPublic(
            key="observability_retention",
            passed=external_retention.qualified,
            reason_code=external_retention.reason_code,
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=external_retention.evidence_id,
                observed_at=observed_at,
                source="stored_policy",
                proxy_retention_days=external_retention.proxy_days,
                container_retention_days=external_retention.container_days,
                apm_retention_days=external_retention.apm_days,
                retention_evidence=external_retention.evidence,
                retention_evidence_id=external_retention.evidence_id,
            ),
        ),
        ClinicPreflightCheckPublic(
            key="formulary",
            passed=formulary_ok,
            reason_code=(None if formulary_ok else "formulary_template_unqualified"),
            evidence=ClinicPreflightEvidencePublic(
                evidence_id=f"stored-policy:formulary:{body.formulary_template}",
                observed_at=observed_at,
                source="stored_policy",
                formulary_template=body.formulary_template,
            ),
        ),
        ClinicPreflightCheckPublic(
            key="calibration",
            passed=body.calibration_required,
            reason_code=(
                None if body.calibration_required else "calibration_gate_required"
            ),
            evidence=ClinicPreflightEvidencePublic(
                evidence_id="stored-policy:calibration-gate:v1",
                observed_at=observed_at,
                source="stored_policy",
                calibration_required=body.calibration_required,
            ),
        ),
    ]
    ready = all(item.passed for item in checks)
    clinic_id = uuid.uuid5(uuid.NAMESPACE_URL, f"nightingale-clinic:{code}:{slug}")
    setting = ClinicOperationalSetting(
        clinic_id=clinic_id,
        timezone=body.timezone,
        worker_enabled=body.worker_enabled,
        supported_languages_json=sorted(languages),
        messaging_channels_json=sorted(messaging),
        remote_text_egress_enabled=body.remote_text_egress_enabled,
        remote_audio_egress_enabled=body.remote_audio_egress_enabled,
        calibration_required=body.calibration_required,
        external_proxy_retention_days=external_retention.proxy_days,
        external_container_retention_days=external_retention.container_days,
        external_apm_retention_days=external_retention.apm_days,
        external_observability_retention_evidence=external_retention.evidence,
        external_observability_retention_evidence_id=external_retention.evidence_id,
        formulary_template=body.formulary_template,
        onboarding_status="ready" if ready else "blocked",
    )
    return ClinicPreflightPublic(
        clinic_id=clinic_id,
        ready=ready,
        checks=checks,
        settings=_setting_public(setting),
    )


@router.post("/auth/login", response_model=Token)
def platform_login(
    body: PlatformLogin, response: Response, session: SessionDep
) -> Token:
    normalized_email = str(body.email).strip().lower()
    if session.get_bind().dialect.name == "postgresql":
        row = (
            session.connection()
            .execute(
                text("SELECT * FROM app_lookup_platform_user(:email)"),
                {"email": normalized_email},
            )
            .one_or_none()
        )
        if row is not None:
            user_id = uuid.UUID(str(row.user_id))
            administrator_id = uuid.UUID(str(row.administrator_id))
            set_rls_actor(session, user_id, role="platform_admin")
            user = session.get(User, user_id)
            administrator = session.get(PlatformAdministrator, administrator_id)
        else:
            user = None
            administrator = None
    else:
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
        if user.hashed_password is not None:
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
    if context.user.email is None:
        raise HTTPException(status_code=409, detail="Platform email is unavailable")
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


@router.post("/clinics/preflight", response_model=ClinicPreflightPublic)
def clinic_onboarding_preflight(
    body: ClinicOnboardingCreate,
    session: SessionDep,
    context: PlatformContextDep,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> ClinicPreflightPublic:
    result = _preflight(session, body)
    _audit(
        session,
        context,
        action="platform.clinic_onboarding_preflight",
        request_id=request_id,
        reason_code="preflight_ready" if result.ready else "preflight_blocked",
    )
    session.commit()
    return result


@router.post(
    "/clinics/onboard",
    response_model=PlatformClinicPublic,
    status_code=201,
)
def onboard_clinic(
    body: ClinicOnboardingCreate,
    session: SessionDep,
    context: PlatformContextDep,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=200)
    ] = None,
) -> PlatformClinicPublic:
    """Create clinic B entirely from data and audited operational settings."""

    code = body.code.strip().upper()
    slug = body.slug.strip().lower()
    clinic_id = uuid.uuid5(uuid.NAMESPACE_URL, f"nightingale-clinic:{code}:{slug}")
    external_retention = external_observability_retention_capability(settings)
    if not external_retention.qualified:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CLINIC_PREFLIGHT_BLOCKED",
                "checks": [
                    ClinicPreflightCheckPublic(
                        key="observability_retention",
                        passed=False,
                        reason_code=external_retention.reason_code,
                        evidence=ClinicPreflightEvidencePublic(
                            evidence_id=external_retention.evidence_id,
                            observed_at=get_datetime_utc(),
                            source="stored_policy",
                            proxy_retention_days=external_retention.proxy_days,
                            container_retention_days=external_retention.container_days,
                            apm_retention_days=external_retention.apm_days,
                            retention_evidence=external_retention.evidence,
                            retention_evidence_id=external_retention.evidence_id,
                        ),
                    ).model_dump(mode="json")
                ],
            },
        )
    replay = session.get(Clinic, clinic_id)
    if replay is not None:
        set_rls_clinic(session, replay.id)
        # The deterministic clinic id is the idempotency boundary. A repeated
        # request returns only when the complete data/configuration intent
        # matches. Silently ignoring changed operational policy or initial
        # staff would leave a nominally ready clinic with the wrong controls.
        operational = session.exec(
            select(ClinicOperationalSetting).where(
                ClinicOperationalSetting.clinic_id == replay.id
            )
        ).first()
        existing_staff = session.exec(
            select(ClinicInvitation).where(ClinicInvitation.clinic_id == replay.id)
        ).all()
        requested_staff = {
            (
                str(item.email).strip().lower(),
                item.full_name.strip(),
                item.role,
            )
            for item in body.initial_staff
        }
        persisted_staff = {
            (str(item.email).strip().lower(), item.invited_full_name, item.role)
            for item in existing_staff
        }
        operational_matches = operational is not None and (
            operational.timezone == body.timezone
            and operational.worker_enabled == body.worker_enabled
            and operational.supported_languages_json
            == sorted(set(body.supported_languages))
            and operational.messaging_channels_json
            == sorted(set(body.messaging_channels))
            and operational.remote_text_egress_enabled
            == body.remote_text_egress_enabled
            and operational.remote_audio_egress_enabled
            == body.remote_audio_egress_enabled
            and operational.calibration_required == body.calibration_required
            and operational.external_proxy_retention_days
            == external_retention.proxy_days
            and operational.external_container_retention_days
            == external_retention.container_days
            and operational.external_apm_retention_days == external_retention.apm_days
            and operational.external_observability_retention_evidence
            == external_retention.evidence
            and operational.external_observability_retention_evidence_id
            == external_retention.evidence_id
            and operational.formulary_template == body.formulary_template
        )
        if (
            replay.code != code
            or replay.slug != slug
            or replay.name != body.name.strip()
            or not operational_matches
            or persisted_staff != requested_staff
        ):
            raise HTTPException(status_code=409, detail="Clinic onboarding conflict")
        member_count = session.exec(
            select(func.count())
            .select_from(ClinicMembership)
            .where(
                ClinicMembership.clinic_id == replay.id,
                col(ClinicMembership.is_active).is_(True),
            )
        ).one()
        patient_count = session.exec(
            select(func.count())
            .select_from(Patient)
            .where(
                Patient.clinic_id == replay.id,
                Patient.status == "active",
            )
        ).one()
        _audit(
            session,
            context,
            action="platform.clinic_onboarding_replayed",
            request_id=request_id,
            clinic_id=replay.id,
            reason_code="idempotent_replay",
        )
        session.commit()
        return PlatformClinicPublic(
            id=replay.id,
            code=replay.code,
            name=replay.name,
            member_count=int(member_count),
            patient_count=int(patient_count),
        )

    result = _preflight(session, body)
    if not result.ready:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CLINIC_PREFLIGHT_BLOCKED",
                "checks": [item.model_dump(mode="json") for item in result.checks],
            },
        )

    set_rls_clinic(session, clinic_id)
    set_rls_actor(session, context.user.id, role="platform_admin")
    clinic = Clinic(id=clinic_id, code=code, slug=slug, name=body.name.strip())
    session.add(clinic)
    session.flush()
    operational = ClinicOperationalSetting(
        clinic_id=clinic.id,
        timezone=body.timezone,
        worker_enabled=body.worker_enabled,
        supported_languages_json=sorted(set(body.supported_languages)),
        messaging_channels_json=sorted(set(body.messaging_channels)),
        remote_text_egress_enabled=body.remote_text_egress_enabled,
        remote_audio_egress_enabled=body.remote_audio_egress_enabled,
        calibration_required=body.calibration_required,
        external_proxy_retention_days=result.settings.external_proxy_retention_days,
        external_container_retention_days=(
            result.settings.external_container_retention_days
        ),
        external_apm_retention_days=result.settings.external_apm_retention_days,
        external_observability_retention_evidence=(
            result.settings.external_observability_retention_evidence
        ),
        external_observability_retention_evidence_id=(
            result.settings.external_observability_retention_evidence_id
        ),
        formulary_template=result.settings.formulary_template,
        onboarding_status="ready",
        updated_by_platform_admin_id=context.administrator.id,
    )
    session.add(operational)

    worker_id = uuid.uuid5(clinic.id, "nightingale-worker")
    worker = User(
        id=worker_id,
        email=f"worker-{clinic.id.hex[:12]}@nightingale.invalid",
        full_name=f"Nightingale Worker ({slug})",
        hashed_password=security.get_password_hash(secrets.token_urlsafe(48)),
        account_kind="service",
    )
    session.add(worker)
    session.flush()
    worker_membership = ClinicMembership(
        clinic_id=clinic.id,
        user_id=worker.id,
        role="worker",
    )
    session.add(worker_membership)
    session.flush()

    try:
        seed_clinic_formulary_template(
            session,
            clinic_id=clinic.id,
            template=body.formulary_template,
        )
    except FormularyConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CLINIC_FORMULARY_TEMPLATE_INVALID",
                "reason_code": exc.code.lower(),
            },
        ) from exc
    _audit(
        session,
        context,
        action="platform.clinic_formulary_seeded",
        request_id=request_id,
        clinic_id=clinic.id,
        reason_code="clinic_formulary_template_qualified",
    )

    now = get_datetime_utc()
    notification_ids: list[uuid.UUID] = []
    for index, initial_staff in enumerate(body.initial_staff):
        raw_token = f"{clinic.id}.{secrets.token_urlsafe(32)}"
        invitation = ClinicInvitation(
            clinic_id=clinic.id,
            email=str(initial_staff.email).strip().lower(),
            invited_full_name=initial_staff.full_name.strip(),
            role=initial_staff.role,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            created_by_platform_admin_id=context.administrator.id,
            expires_at=now + timedelta(hours=24),
        )
        session.add(invitation)
        session.flush()
        try:
            notification, _ = queue_notification(
                session,
                clinic_id=clinic.id,
                purpose="staff_invitation",
                channel="email",
                destination=str(initial_staff.email).strip().lower(),
                template_key="staff-invitation-v1",
                payload={
                    "invitation_token": raw_token,
                    "clinic_code": clinic.code,
                    "role": initial_staff.role,
                },
                idempotency_key=(
                    f"clinic-onboarding:{clinic.id}:{index}:"
                    f"{idempotency_key or invitation.id}"
                ),
            )
        except NotificationChannelUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "CLINIC_MESSAGING_CAPABILITY_CHANGED"},
            ) from exc
        notification_ids.append(notification.id)

    _audit(
        session,
        context,
        action="platform.clinic_onboarded",
        request_id=request_id,
        clinic_id=clinic.id,
        reason_code="clinic_preflight_passed",
    )
    session.commit()
    if not bind_notification_worker(session, clinic.id):
        raise HTTPException(status_code=503, detail="Delivery worker unavailable")
    for notification_id in notification_ids:
        persisted_notification = session.get(NotificationOutbox, notification_id)
        if persisted_notification is not None:
            dispatch_notification(session, persisted_notification)
    session.commit()
    return PlatformClinicPublic(
        id=clinic.id,
        code=clinic.code,
        name=clinic.name,
        member_count=1,
        patient_count=0,
    )


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
