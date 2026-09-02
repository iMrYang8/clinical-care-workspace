"""Nightingale database models and public API schemas.

Every domain row carries a clinic identifier. Application query scoping and the
PostgreSQL RLS migration are independent tenant boundaries.
"""

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import EmailStr, StringConstraints
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from sqlmodel._compat import SQLModelConfig


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


Role = Literal["patient", "staff", "clinician", "admin", "worker"]
ReasonCodeInput = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]{2,79}$",
    ),
]
LiveTranscriptStatus = Literal[
    "not_started", "available", "unavailable", "needs_review", "replaced"
]
# Clinic invitations provision care-team access only. Patient identities require
# an explicit PatientUserLink onboarding flow so an admin cannot accidentally
# create an unlinked patient account from the membership screen.
MembershipRole = Literal["staff", "clinician", "admin"]
Section = Literal["patient", "staff", "clinician", "system"]
EntryOrigin = Literal["human", "ai", "system"]
VisitStatus = Literal[
    "scheduled", "checked_in", "in_progress", "completed", "cancelled", "no_show"
]
InteractionType = Literal[
    "care_note",
    "doctor_consult",
    "patient_insight",
    "voice_session",
]
EntryType = Literal[
    "manual_staff_note",
    "manual_clinician_note",
    "manual_patient_insight",
    "ai_doctor_consult_summary",
    "ai_nurse_consult_summary",
    "ai_patient_session_summary",
    "voice_transcript_source",
    "voice_reviewed_result",
    "legacy_review_required",
    "system_record",
]


class RiskReason(StrEnum):
    """Exhaustive, PHI-free reason codes exposed by ranking projections."""

    CRITICAL = "critical"
    UNRESOLVED = "unresolved"
    CLINICIAN_CONFIRMED = "clinician_confirmed"
    CLINICAL_ENTITY = "clinical_entity"
    CLINIC_FEEDBACK = "clinic_feedback"
    RECENCY = "recency"
    CLINICIAN_ACCEPTED = "clinician_accepted"
    CARE_PLAN_CONFLICT = "care_plan_conflict"
    CLINICIAN_CONFIRMED_FOLLOW_UP = "clinician_confirmed_follow_up"
    MEDICATION_STATUS_CONFLICT = "medication_status_conflict"
    OPEN_MEDICATION_RECONCILIATION = "open_medication_reconciliation"
    SCHEDULED_FOLLOW_UP = "scheduled_follow_up"
    SYNTHETIC_DATASET_RECENT_ENCOUNTER = "synthetic_dataset_recent_encounter"
    UNAVAILABLE_REVIEW_REQUIRED = "unavailable_review_required"


def normalize_risk_reason(value: str) -> RiskReason:
    """Fail closed when legacy or malformed persisted reason codes are read."""

    try:
        return RiskReason(value)
    except ValueError:
        return RiskReason.UNAVAILABLE_REVIEW_REQUIRED


class TenantRow(SQLModel):
    clinic_id: uuid.UUID = Field(foreign_key="clinics.id", ondelete="CASCADE")


class Clinic(SQLModel, table=True):
    __tablename__ = "clinics"
    __table_args__ = (
        CheckConstraint(
            "code ~ '^[A-Z]{3,12}$'",
            name="ck_clinics_code_format",
        ),
        UniqueConstraint("code", name="clinics_code_key"),
        UniqueConstraint("slug", name="clinics_slug_key"),
        Index("ix_clinics_code", "code"),
        Index("ix_clinics_slug", "slug"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    code: str = Field(max_length=12)
    slug: str = Field(max_length=80)
    name: str = Field(max_length=255)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "account_kind IN ('staff','patient','service')",
            name="ck_users_account_kind",
        ),
        CheckConstraint(
            "account_kind = 'patient' OR "
            "(email IS NOT NULL AND hashed_password IS NOT NULL)",
            name="ck_users_staff_credentials",
        ),
        UniqueConstraint("email", name="users_email_key"),
        Index("ix_users_email", "email"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Patient portal identities may be phone/OTP-only.  Human clinic and
    # service accounts remain subject to the database credential check above.
    email: EmailStr | None = Field(default=None, max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    hashed_password: str | None = None
    account_kind: str = Field(default="staff", max_length=20, index=True)
    is_active: bool = True
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ClinicMembership(TenantRow, table=True):
    __tablename__ = "clinic_memberships"
    __table_args__ = (
        UniqueConstraint("clinic_id", "user_id", name="uq_membership_clinic_user"),
        UniqueConstraint("clinic_id", "id", name="uq_membership_clinic_id"),
        Index("ix_membership_user_active", "user_id", "is_active"),
        Index("ix_clinic_memberships_clinic_id", "clinic_id"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="CASCADE")
    role: str = Field(max_length=20)
    is_active: bool = True
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ClinicAISetting(TenantRow, table=True):
    __tablename__ = "clinic_ai_settings"
    __table_args__ = (
        UniqueConstraint("clinic_id", name="uq_clinic_ai_settings_clinic"),
        ForeignKeyConstraint(
            ["clinic_id", "updated_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_ai_settings_updater",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    provider: str = Field(default="openai", max_length=40)
    api_key_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    api_key_last4: str | None = Field(default=None, max_length=4)
    fast_model: str = Field(default="gpt-5-mini", max_length=160)
    careful_model: str = Field(default="gpt-5.1", max_length=160)
    transcribe_model: str = Field(default="gpt-4o-transcribe-diarize", max_length=160)
    updated_by_membership_id: uuid.UUID
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ClinicInvitation(TenantRow, table=True):
    __tablename__ = "clinic_invitations"
    __table_args__ = (
        CheckConstraint(
            "(created_by_membership_id IS NOT NULL)::int + "
            "(created_by_platform_admin_id IS NOT NULL)::int = 1",
            name="ck_clinic_invitation_exactly_one_creator",
        ),
        UniqueConstraint("token_hash", name="clinic_invitations_token_hash_key"),
        Index(
            "ix_clinic_invitations_pending_email",
            "clinic_id",
            "email",
            "accepted_at",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_invitation_creator_tenant",
        ),
        ForeignKeyConstraint(
            ["created_by_platform_admin_id"],
            ["platform_administrators.id"],
            name="fk_clinic_invitation_platform_creator",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: EmailStr = Field(max_length=255)
    invited_full_name: str | None = Field(default=None, max_length=255)
    role: str = Field(max_length=20)
    token_hash: str = Field(max_length=64)
    created_by_membership_id: uuid.UUID | None = None
    created_by_platform_admin_id: uuid.UUID | None = None
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    accepted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Patient(TenantRow, table=True):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_patient_clinic_id"),
        Index("ix_patient_clinic_created", "clinic_id", "created_at"),
        ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_creator_membership",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    display_name_ciphertext: bytes = Field(
        sa_column=Column(LargeBinary, nullable=False)
    )
    external_ref_hash: str = Field(max_length=64, index=True)
    date_of_birth_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    identity_match_hash: str | None = Field(default=None, max_length=64, index=True)
    status: str = Field(default="active", max_length=20)
    created_by_membership_id: uuid.UUID | None = None
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientVisit(TenantRow, table=True):
    __tablename__ = "patient_visits"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled','checked_in','in_progress','completed','cancelled','no_show')",
            name="ck_patient_visits_status",
        ),
        UniqueConstraint("clinic_id", "id", name="uq_patient_visit_clinic_id"),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_visit_patient",
            ondelete="CASCADE",
        ),
        Index(
            "ix_patient_visit_clinic_schedule",
            "clinic_id",
            "scheduled_at",
            "status",
        ),
        Index("ix_patient_visit_patient", "clinic_id", "patient_id"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    scheduled_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    status: str = Field(default="scheduled", max_length=20)
    visit_type: str = Field(default="clinic_visit", max_length=40)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientUserLink(TenantRow, table=True):
    __tablename__ = "patient_user_links"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "patient_id", "user_id", name="uq_patient_user_link"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_link_tenant",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="CASCADE")
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientIdentifier(TenantRow, table=True):
    __tablename__ = "patient_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "identifier_type", "value_hmac", name="uq_patient_identifier"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_identifier_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_identifier_creator",
        ),
        Index("ix_patient_identifier_patient", "clinic_id", "patient_id"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    identifier_type: str = Field(max_length=40)
    value_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    value_hmac: str = Field(max_length=64)
    masked_suffix: str = Field(max_length=8)
    created_by_membership_id: uuid.UUID
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientPortalInvitation(TenantRow, table=True):
    __tablename__ = "patient_portal_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="patient_portal_invitation_token_key"),
        UniqueConstraint(
            "clinic_id", "id", name="uq_patient_portal_invitation_clinic_id"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_portal_invitation_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_portal_invitation_creator",
        ),
        Index(
            "ix_patient_portal_invitation_pending",
            "clinic_id",
            "patient_id",
            "accepted_at",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    # Legacy email invitations remain valid; phone/OTP enrollment intentionally
    # carries no invented email address.
    email: EmailStr | None = Field(default=None, max_length=255)
    token_hash: str = Field(max_length=64)
    created_by_membership_id: uuid.UUID
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    accepted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientAccessCredential(TenantRow, table=True):
    """Revocable patient portal identity independent of an email address.

    Phone values are encrypted for display and HMACed only for controlled
    matching.  ``phone_hmac`` is deliberately not unique: households may share
    a number, and number reassignment creates a new credential while preserving
    the revoked historical record.
    """

    __tablename__ = "patient_access_credentials"
    __table_args__ = (
        CheckConstraint(
            "preferred_channel IN ('sms','whatsapp')",
            name="ck_patient_access_preferred_channel",
        ),
        UniqueConstraint("clinic_id", "id", name="uq_patient_access_credential_id"),
        UniqueConstraint("portal_id", name="patient_access_portal_id_key"),
        UniqueConstraint("claim_code_hash", name="patient_access_claim_code_key"),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_access_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "invitation_id"],
            ["patient_portal_invitations.clinic_id", "patient_portal_invitations.id"],
            name="fk_patient_access_invitation",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_access_creator",
        ),
        Index("ix_patient_access_phone_hmac", "clinic_id", "phone_hmac"),
        Index(
            "uq_patient_access_active_patient",
            "clinic_id",
            "patient_id",
            unique=True,
            postgresql_where=text("is_active AND revoked_at IS NULL"),
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    user_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="SET NULL"
    )
    invitation_id: uuid.UUID | None = None
    portal_id: str = Field(max_length=80)
    phone_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    phone_hmac: str | None = Field(default=None, max_length=64)
    masked_phone: str | None = Field(default=None, max_length=32)
    preferred_channel: str = Field(default="sms", max_length=20)
    claim_code_hash: str = Field(max_length=64)
    claim_code_expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    claim_code_used_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_by_membership_id: uuid.UUID | None = None
    is_active: bool = True
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    recovery_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientOTPChallenge(TenantRow, table=True):
    __tablename__ = "patient_otp_challenges"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('enrollment','login','recovery','phone_change')",
            name="ck_patient_otp_purpose",
        ),
        UniqueConstraint("challenge_token_hash", name="patient_otp_token_hash_key"),
        ForeignKeyConstraint(
            ["clinic_id", "credential_id"],
            ["patient_access_credentials.clinic_id", "patient_access_credentials.id"],
            name="fk_patient_otp_credential",
            ondelete="CASCADE",
        ),
        Index(
            "ix_patient_otp_active",
            "clinic_id",
            "credential_id",
            "expires_at",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    credential_id: uuid.UUID
    purpose: str = Field(max_length=20)
    challenge_token_hash: str = Field(max_length=64)
    otp_hash: str = Field(max_length=255)
    attempts_remaining: int = Field(default=5, ge=0)
    resend_available_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    consumed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ClinicOperationalSetting(TenantRow, table=True):
    __tablename__ = "clinic_operational_settings"
    __table_args__ = (
        UniqueConstraint("clinic_id", name="uq_clinic_operational_setting"),
        CheckConstraint(
            "onboarding_status IN ('draft','ready','blocked')",
            name="ck_clinic_operational_onboarding",
        ),
        CheckConstraint(
            "external_proxy_retention_days BETWEEN 1 AND 30",
            name="ck_clinic_external_proxy_retention",
        ),
        CheckConstraint(
            "external_container_retention_days BETWEEN 1 AND 30",
            name="ck_clinic_external_container_retention",
        ),
        CheckConstraint(
            "external_apm_retention_days BETWEEN 1 AND 30",
            name="ck_clinic_external_apm_retention",
        ),
        CheckConstraint(
            "external_observability_retention_evidence IN "
            "('unqualified','deterministic_fixture','deployment_policy','provider_contract')",
            name="ck_clinic_external_retention_evidence",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    timezone: str = Field(default="Asia/Singapore", max_length=80)
    worker_enabled: bool = False
    supported_languages_json: list[str] = Field(
        default_factory=lambda: ["en", "ms", "nan", "zh"],
        sa_column=Column(JSONB, nullable=False),
    )
    messaging_channels_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    remote_text_egress_enabled: bool = False
    remote_audio_egress_enabled: bool = False
    calibration_required: bool = True
    external_proxy_retention_days: int = 30
    external_container_retention_days: int = 30
    external_apm_retention_days: int = 30
    external_observability_retention_evidence: str = Field(
        default="deterministic_fixture", max_length=40
    )
    external_observability_retention_evidence_id: str = Field(
        default="fixture:nightingale:external-observability-30d", max_length=200
    )
    formulary_template: str = Field(
        default="nightingale-clinic-formulary-v1", max_length=80
    )
    onboarding_status: str = Field(default="draft", max_length=20)
    updated_by_platform_admin_id: uuid.UUID | None = Field(
        default=None, foreign_key="platform_administrators.id"
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class WorkerHeartbeat(SQLModel, table=True):
    """Deployment capability signal shared by platform onboarding checks."""

    __tablename__ = "worker_heartbeats"
    __table_args__ = (UniqueConstraint("worker_kind", name="uq_worker_heartbeat_kind"),)
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    worker_kind: str = Field(max_length=40)
    worker_version: str = Field(max_length=80)
    source_commit: str = Field(default="unknown", max_length=64)
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ClinicFormularyVersion(TenantRow, table=True):
    """Immutable clinic-approved medication screening configuration."""

    __tablename__ = "clinic_formulary_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','retired')",
            name="ck_clinic_formulary_version_status",
        ),
        UniqueConstraint("clinic_id", "id", name="uq_clinic_formulary_version_id"),
        UniqueConstraint(
            "clinic_id", "version_code", name="uq_clinic_formulary_version_code"
        ),
        Index("ix_clinic_formulary_active", "clinic_id", "status", "effective_at"),
        Index(
            "uq_clinic_formulary_one_active",
            "clinic_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint(
            "status != 'active' OR qualified_at IS NOT NULL",
            name="ck_clinic_formulary_active_qualified",
        ),
        CheckConstraint(
            "qualification_source IS NULL OR "
            "qualification_source IN ('clinic_admin','platform_template')",
            name="ck_clinic_formulary_qualification_source",
        ),
        CheckConstraint(
            "(qualified_at IS NULL AND qualified_by_membership_id IS NULL "
            "AND qualification_source IS NULL) OR "
            "(qualified_at IS NOT NULL AND qualification_source = 'clinic_admin' "
            "AND qualified_by_membership_id IS NOT NULL) OR "
            "(qualified_at IS NOT NULL AND qualification_source = 'platform_template' "
            "AND qualified_by_membership_id IS NULL)",
            name="ck_clinic_formulary_qualification_actor",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_formulary_creator",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "qualified_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_formulary_qualifier",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "activated_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_formulary_activator",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    version_code: str = Field(max_length=80)
    status: str = Field(default="draft", max_length=20)
    content_sha256: str = Field(max_length=64)
    effective_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    created_by_membership_id: uuid.UUID | None = None
    content_locked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    qualified_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    qualified_by_membership_id: uuid.UUID | None = None
    qualification_source: str | None = Field(default=None, max_length=30)
    activated_by_membership_id: uuid.UUID | None = None
    retired_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ClinicFormularyConcept(TenantRow, table=True):
    __tablename__ = "clinic_formulary_concepts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["clinic_id", "formulary_version_id"],
            ["clinic_formulary_versions.clinic_id", "clinic_formulary_versions.id"],
            name="fk_clinic_formulary_concept_version",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "clinic_id",
            "formulary_version_id",
            "concept_code",
            name="uq_clinic_formulary_concept",
        ),
        Index(
            "ix_clinic_formulary_concept_version",
            "clinic_id",
            "formulary_version_id",
        ),
        CheckConstraint(
            "minimum_single_dose > 0 AND maximum_single_dose >= minimum_single_dose",
            name="ck_clinic_formulary_dose_range",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    formulary_version_id: uuid.UUID
    concept_code: str = Field(max_length=100)
    canonical_name: str = Field(max_length=255)
    multilingual_aliases_json: dict[str, list[str]] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    dose_unit: str = Field(max_length=20)
    minimum_single_dose: float
    maximum_single_dose: float
    permitted_routes_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    contraindicated_allergy_concepts_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    active: bool = True


class PlatformAdministrator(SQLModel, table=True):
    __tablename__ = "platform_administrators"
    __table_args__ = (UniqueConstraint("user_id", name="platform_admin_user_key"),)
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="CASCADE")
    is_active: bool = True
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PlatformAuditEvent(SQLModel, table=True):
    __tablename__ = "platform_audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_clinic_id", "target_patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_platform_audit_patient",
        ),
        Index("ix_platform_audit_created", "created_at"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    platform_admin_id: uuid.UUID = Field(foreign_key="platform_administrators.id")
    action: str = Field(max_length=100)
    target_clinic_id: uuid.UUID | None = Field(default=None, foreign_key="clinics.id")
    target_patient_id: uuid.UUID | None = None
    request_id: str = Field(max_length=100)
    reason_code: str = Field(default="not_specified", max_length=80)
    clinical_rationale_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    metadata_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class NotificationOutbox(TenantRow, table=True):
    """Transactional intent for invitation, appointment, and correction delivery."""

    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','submitted','delivered','failed',"
            "'acknowledged','revoked')",
            name="ck_notification_outbox_state",
        ),
        CheckConstraint(
            "channel IN ('email','sms','whatsapp','portal')",
            name="ck_notification_outbox_channel",
        ),
        UniqueConstraint("clinic_id", "id", name="uq_notification_outbox_id"),
        UniqueConstraint(
            "clinic_id", "idempotency_key", name="uq_notification_idempotency"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_notification_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "visit_id"],
            ["patient_visits.clinic_id", "patient_visits.id"],
            name="fk_notification_visit",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_notification_publication",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "portal_invitation_id"],
            ["patient_portal_invitations.clinic_id", "patient_portal_invitations.id"],
            name="fk_notification_portal_invitation",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_notification_creator",
        ),
        Index(
            "ix_notification_dispatch",
            "clinic_id",
            "state",
            "available_at",
        ),
        Index("ix_notification_patient", "clinic_id", "patient_id", "created_at"),
        Index(
            "ix_notification_portal_invitation",
            "clinic_id",
            "portal_invitation_id",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID | None = None
    visit_id: uuid.UUID | None = None
    publication_id: uuid.UUID | None = None
    portal_invitation_id: uuid.UUID | None = None
    purpose: str = Field(max_length=40)
    channel: str = Field(max_length=20)
    destination_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    destination_masked: str = Field(max_length=120)
    template_key: str = Field(max_length=80)
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    idempotency_key: str = Field(max_length=200)
    state: str = Field(default="queued", max_length=20)
    created_by_membership_id: uuid.UUID | None = None
    available_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    submitted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    delivered_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    failed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    acknowledged_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class NotificationAttempt(TenantRow, table=True):
    __tablename__ = "notification_attempts"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "notification_id", "attempt_no", name="uq_notification_attempt"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_notification_attempt_outbox",
            ondelete="CASCADE",
        ),
        Index(
            "ix_notification_attempt_message",
            "clinic_id",
            "provider",
            "provider_message_id",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    notification_id: uuid.UUID
    attempt_no: int = Field(ge=1)
    provider: str = Field(max_length=60)
    provider_message_id: str | None = Field(default=None, max_length=200)
    request_sha256: str = Field(max_length=64)
    status: str = Field(default="started", max_length=30)
    error_class: str | None = Field(default=None, max_length=80)
    error_code: str | None = Field(default=None, max_length=80)
    started_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class NotificationReceipt(TenantRow, table=True):
    __tablename__ = "notification_receipts"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id",
            "provider",
            "provider_event_id",
            name="uq_notification_receipt_event",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_notification_receipt_outbox",
            ondelete="CASCADE",
        ),
        Index("ix_notification_receipt_outbox", "clinic_id", "notification_id"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    notification_id: uuid.UUID
    provider: str = Field(max_length=60)
    provider_event_id: str = Field(max_length=200)
    provider_message_id: str = Field(max_length=200)
    event_type: str = Field(max_length=40)
    signature_verified: bool = False
    payload_sha256: str = Field(max_length=64)
    occurred_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    received_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientPublicationAcknowledgement(TenantRow, table=True):
    __tablename__ = "patient_publication_acknowledgements"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id",
            "publication_id",
            "acknowledged_by_user_id",
            "event_type",
            name="uq_patient_publication_ack",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_publication_ack_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_publication_ack_publication",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_publication_ack_notification",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    publication_id: uuid.UUID
    notification_id: uuid.UUID | None = None
    acknowledged_by_user_id: uuid.UUID = Field(foreign_key="users.id")
    channel: str = Field(default="portal", max_length=20)
    event_type: str = Field(default="acknowledged", max_length=40)
    acknowledged_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PublicationCorrectionOutreach(TenantRow, table=True):
    __tablename__ = "publication_correction_outreaches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_correction_outreach_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "withdrawn_publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_correction_outreach_withdrawn",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "replacement_publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_correction_outreach_replacement",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_correction_outreach_notification",
        ),
        UniqueConstraint(
            "clinic_id",
            "withdrawn_publication_id",
            "replacement_publication_id",
            "notification_id",
            name="uq_correction_outreach_delivery",
        ),
        Index("ix_correction_outreach_status", "clinic_id", "status", "due_at"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    withdrawn_publication_id: uuid.UUID
    replacement_publication_id: uuid.UUID | None = None
    notification_id: uuid.UUID | None = None
    status: str = Field(default="pending", max_length=30)
    due_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientPortalEvent(TenantRow, table=True):
    """Durable, encrypted invalidation event used by SSE and polling clients."""

    __tablename__ = "patient_portal_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_portal_event_patient",
            ondelete="CASCADE",
        ),
        Index(
            "ix_patient_portal_event_cursor", "clinic_id", "patient_id", "created_at"
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    event_type: str = Field(max_length=80)
    aggregate_type: str = Field(max_length=40)
    aggregate_id: uuid.UUID
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Entry(TenantRow, table=True):
    __tablename__ = "entries"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_entry_clinic_id"),
        UniqueConstraint(
            "clinic_id", "patient_id", "id", name="uq_entry_clinic_patient_id"
        ),
        Index("ix_entry_patient_section", "clinic_id", "patient_id", "section"),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_entry_patient_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "current_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_entry_current_version_tenant",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "source_job_id", "patient_id"],
            ["jobs.clinic_id", "jobs.id", "jobs.patient_id"],
            name="fk_entry_source_job_patient_tenant",
            use_alter=True,
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    section: str = Field(max_length=20)
    origin: str = Field(default="human", max_length=20)
    entry_type: str = Field(default="legacy_review_required", max_length=60, index=True)
    patient_facing: bool = Field(default=False)
    source_job_id: uuid.UUID | None = Field(default=None, index=True)
    current_version_id: uuid.UUID | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    occurred_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )


class EntryVersion(TenantRow, table=True):
    __tablename__ = "entry_versions"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_entry_version_clinic_id"),
        UniqueConstraint(
            "clinic_id", "entry_id", "id", name="uq_entry_version_clinic_entry_id"
        ),
        UniqueConstraint("entry_id", "version_no", name="uq_entry_version_number"),
        Index("ix_entry_version_entry_created", "entry_id", "created_at"),
        ForeignKeyConstraint(
            ["clinic_id", "entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_version_entry_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "reverted_from_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_version_reverted_from_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "archive_blob_id"],
            ["archive_blobs.clinic_id", "archive_blobs.id"],
            name="fk_version_archive_blob_tenant",
            use_alter=True,
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entry_id: uuid.UUID
    version_no: int = Field(ge=1)
    title_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    content_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    content_sha256: str = Field(max_length=64)
    patient_facing: bool = Field(default=False, index=True)
    author_id: uuid.UUID = Field(foreign_key="users.id")
    reverted_from_version_id: uuid.UUID | None = None
    storage_tier: str = Field(default="hot", max_length=10)
    archive_blob_id: uuid.UUID | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class EntryRelation(TenantRow, table=True):
    __tablename__ = "entry_relations"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id",
            "source_entry_id",
            "target_entry_id",
            "relation_type",
            name="uq_entry_relation",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "source_entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_relation_source",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "target_entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_relation_target",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_entry_id: uuid.UUID
    target_entry_id: uuid.UUID
    relation_type: str = Field(max_length=40)
    created_by_id: uuid.UUID = Field(foreign_key="users.id")
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Comment(TenantRow, table=True):
    __tablename__ = "comments"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_comment_revision_positive"),
        UniqueConstraint("clinic_id", "id", name="uq_comment_clinic_id"),
        Index("ix_comment_entry_status", "entry_id", "resolved_at"),
        ForeignKeyConstraint(
            ["clinic_id", "entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_comment_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_comment_version",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "parent_id"],
            ["comments.clinic_id", "comments.id"],
            name="fk_comment_parent_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "assigned_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_comment_assignment_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    parent_id: uuid.UUID | None = Field(default=None)
    author_id: uuid.UUID = Field(foreign_key="users.id")
    body_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    start_offset: int | None = None
    end_offset: int | None = None
    exact_quote_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    prefix_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    suffix_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    quote_sha256: str | None = Field(default=None, max_length=64)
    anchor_state: str = Field(default="resolved", max_length=20)
    review_required: bool = False
    assigned_membership_id: uuid.UUID | None = Field(default=None)
    revision: int = Field(default=1, ge=1)
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class CommentMention(TenantRow, table=True):
    __tablename__ = "comment_mentions"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "comment_id", "mentioned_user_id", name="uq_comment_mention"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "comment_id"],
            ["comments.clinic_id", "comments.id"],
            name="fk_comment_mention_tenant",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    comment_id: uuid.UUID
    mentioned_user_id: uuid.UUID = Field(foreign_key="users.id")
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class CareTask(TenantRow, table=True):
    __tablename__ = "care_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_task_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "comment_id"],
            ["comments.clinic_id", "comments.id"],
            name="fk_task_comment_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "assignee_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_task_assignee_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    comment_id: uuid.UUID | None = Field(default=None)
    assignee_membership_id: uuid.UUID
    status: str = Field(default="open", max_length=20)
    title_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Highlight(TenantRow, table=True):
    __tablename__ = "highlights"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_highlight_clinic_id"),
        UniqueConstraint(
            "clinic_id",
            "candidate_fingerprint",
            name="uq_highlight_candidate_fingerprint",
        ),
        CheckConstraint(
            "candidate_fingerprint IS NULL OR candidate_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_highlight_candidate_fingerprint_sha256",
        ),
        Index(
            "ix_highlight_candidate_fingerprint",
            "clinic_id",
            "candidate_fingerprint",
        ),
        CheckConstraint(
            "support_state IN ('current','historical','superseded')",
            name="ck_highlight_support_state_value",
        ),
        CheckConstraint(
            "NOT support_review_required OR NOT current_priority_eligible",
            name="ck_highlight_review_removes_priority",
        ),
        CheckConstraint(
            "NOT (feature_keys_json @> '[\"entity:allergy\"]'::jsonb) "
            "OR learned_score >= 0",
            name="ck_highlight_allergy_learning_floor",
        ),
        CheckConstraint(
            "NOT (feature_keys_json @> '[\"entity:medication\"]'::jsonb) "
            "OR learned_score >= 0",
            name="ck_highlight_medication_learning_floor",
        ),
        Index("ix_highlight_patient_status", "patient_id", "status", "pinned"),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_highlight_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_highlight_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "source_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_highlight_version",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    source_entry_version_id: uuid.UUID
    # Stable, non-PHI identity for one model candidate on one immutable source
    # span. Human-authored highlights remain NULL and are never coalesced into
    # an AI candidate merely because their wording happens to match.
    candidate_fingerprint: str | None = Field(default=None, max_length=64)
    label_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    status: str = Field(default="pending", max_length=20)
    pinned: bool = Field(default=False)
    critical: bool = Field(default=False)
    patient_facing: bool = Field(default=False)
    anchor_state: str = Field(default="resolved", max_length=20)
    review_required: bool = False
    feature_keys_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    base_score: float = Field(default=0.0, sa_column=Column(Float, nullable=False))
    learned_score: float = Field(default=0.0, sa_column=Column(Float, nullable=False))
    final_score: float = Field(
        default=0.0, sa_column=Column(Float, nullable=False, index=True)
    )
    risk_reason: str = Field(default="recency", max_length=100)
    unresolved: bool = False
    clinician_confirmed: bool = False
    support_state: str = Field(default="current", max_length=20)
    support_review_required: bool = False
    current_priority_eligible: bool = True
    created_by_id: uuid.UUID = Field(foreign_key="users.id")
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class HighlightSupportReview(TenantRow, table=True):
    """Addressable review created whenever a highlight's source entry changes."""

    __tablename__ = "highlight_support_reviews"
    __table_args__ = (
        CheckConstraint(
            "support_state IN ('current','historical','superseded')",
            name="ck_highlight_support_state",
        ),
        CheckConstraint(
            "review_status IN ('pending','reaffirmed','superseded')",
            name="ck_highlight_support_review_status",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_highlight_support_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_highlight_support_highlight",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "source_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_highlight_support_source",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "observed_current_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_highlight_support_current",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "reviewed_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_highlight_support_reviewer",
        ),
        UniqueConstraint(
            "clinic_id",
            "highlight_id",
            "observed_current_version_id",
            name="uq_highlight_support_observation",
        ),
        Index(
            "ix_highlight_support_pending",
            "clinic_id",
            "patient_id",
            "review_status",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    highlight_id: uuid.UUID
    source_entry_version_id: uuid.UUID
    observed_current_version_id: uuid.UUID
    support_state: str = Field(default="historical", max_length=20)
    review_status: str = Field(default="pending", max_length=20)
    reviewed_by_membership_id: uuid.UUID | None = None
    reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ProvenancePointer(TenantRow, table=True):
    __tablename__ = "provenance_pointers"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_provenance_clinic_id"),
        Index("ix_provenance_version_span", "entry_version_id", "start_offset"),
        ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_provenance_highlight_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "comment_id"],
            ["comments.clinic_id", "comments.id"],
            name="fk_provenance_comment_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_provenance_version",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "audio_asset_id"],
            ["audio_assets.clinic_id", "audio_assets.id"],
            name="fk_provenance_audio_asset_tenant",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["clinic_id", "clinical_fact_id"],
            ["clinical_facts.clinic_id", "clinical_facts.id"],
            name="fk_provenance_clinical_fact_tenant",
            use_alter=True,
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    highlight_id: uuid.UUID | None = Field(default=None)
    comment_id: uuid.UUID | None = Field(default=None)
    clinical_fact_id: uuid.UUID | None = Field(default=None)
    entry_version_id: uuid.UUID
    start_offset: int
    end_offset: int
    exact_quote_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    prefix_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    suffix_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    quote_sha256: str = Field(max_length=64)
    anchor_state: str = Field(default="resolved", max_length=20)
    review_required: bool = False
    audio_asset_id: uuid.UUID | None = None
    audio_start_ms: int | None = None
    audio_end_ms: int | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ConflictCase(TenantRow, table=True):
    __tablename__ = "conflict_cases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_conflict_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "left_entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_conflict_left",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "right_entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_conflict_right",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "left_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_conflict_left_version",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "right_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_conflict_right_version",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "left_pointer_id"],
            ["provenance_pointers.clinic_id", "provenance_pointers.id"],
            name="fk_conflict_left_pointer",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "right_pointer_id"],
            ["provenance_pointers.clinic_id", "provenance_pointers.id"],
            name="fk_conflict_right_pointer",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "left_assertion_id"],
            ["clinical_fact_assertions.clinic_id", "clinical_fact_assertions.id"],
            name="fk_conflict_left_assertion",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "right_assertion_id"],
            ["clinical_fact_assertions.clinic_id", "clinical_fact_assertions.id"],
            name="fk_conflict_right_assertion",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "resolved_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_conflict_resolver",
        ),
        Index(
            "ix_conflict_patient_status",
            "clinic_id",
            "patient_id",
            "status",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    left_entry_id: uuid.UUID
    right_entry_id: uuid.UUID
    fact_type: str = Field(default="clinical", max_length=40)
    normalized_key: str = Field(default="", max_length=200)
    left_version_id: uuid.UUID | None = None
    right_version_id: uuid.UUID | None = None
    left_pointer_id: uuid.UUID | None = None
    right_pointer_id: uuid.UUID | None = None
    left_assertion_id: uuid.UUID | None = Field(default=None)
    right_assertion_id: uuid.UUID | None = Field(default=None)
    severity: str = Field(default="high", max_length=20)
    status: str = Field(default="unresolved", max_length=20)
    resolution: str | None = Field(default=None, max_length=500)
    resolved_by_membership_id: uuid.UUID | None = None
    resolved_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AuditEvent(TenantRow, table=True):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_clinic_created", "clinic_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    actor_id: uuid.UUID = Field(foreign_key="users.id")
    action: str = Field(max_length=80)
    resource_type: str = Field(max_length=40)
    resource_id: uuid.UUID
    reason_code: str = Field(default="not_specified", max_length=80)
    clinical_rationale_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    # Retained as a compatibility envelope for non-clinical, allowlisted
    # machine metadata only.  Human rationale belongs in the encrypted column.
    metadata_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientGlanceSnapshot(TenantRow, table=True):
    __tablename__ = "patient_glance_snapshots"
    __table_args__ = (
        CheckConstraint(
            "importance_mode IN ('disabled','shadow','active')",
            name="ck_patient_glance_importance_mode",
        ),
        UniqueConstraint("clinic_id", "patient_id", name="uq_glance_patient"),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_glance_patient",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    source_event_sequence: int | None = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    generated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    freshness_state: str = Field(default="fresh", max_length=20)
    provider_outage: bool = False
    outage_started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    fallback_kind: str | None = Field(default=None, max_length=30)
    importance_mode: str = Field(default="shadow", max_length=20)
    importance_qualification_report_id: uuid.UUID | None = None
    importance_qualification_report_version: str | None = Field(
        default=None, max_length=80
    )
    importance_qualification_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class DomainEvent(TenantRow, table=True):
    __tablename__ = "domain_events"
    __table_args__ = (
        Index("ix_domain_event_clinic_sequence", "clinic_id", "sequence_no"),
        UniqueConstraint("id", name="domain_events_id_key"),
    )
    sequence_no: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str = Field(max_length=100)
    aggregate_type: str = Field(max_length=40)
    aggregate_id: uuid.UUID
    actor_id: uuid.UUID = Field(foreign_key="users.id")
    payload_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ProviderCircuitState(TenantRow, table=True):
    __tablename__ = "provider_circuit_states"
    __table_args__ = (
        CheckConstraint(
            "state IN ('closed','open','half_open')",
            name="ck_provider_circuit_state",
        ),
        UniqueConstraint(
            "clinic_id", "provider", "capability", name="uq_provider_circuit"
        ),
        Index("ix_provider_circuit_probe", "clinic_id", "state", "next_probe_at"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    provider: str = Field(max_length=80)
    capability: str = Field(max_length=80)
    state: str = Field(default="closed", max_length=20)
    consecutive_failures: int = Field(default=0, ge=0)
    last_error_class: str | None = Field(default=None, max_length=80)
    opened_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    next_probe_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_success_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Job(TenantRow, table=True):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_job_clinic_id"),
        UniqueConstraint(
            "clinic_id", "id", "patient_id", name="uq_job_clinic_id_patient"
        ),
        UniqueConstraint(
            "clinic_id", "kind", "idempotency_key", name="uq_job_idempotency"
        ),
        Index("ix_job_clinic_state_next", "clinic_id", "state", "next_run_at"),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_job_patient_tenant",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    kind: str = Field(max_length=40)
    state: str = Field(default="pending", max_length=30)
    idempotency_key: str = Field(max_length=200)
    request_sha256: str = Field(max_length=64)
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    attempt_count: int = 0
    max_attempts: int = 3
    next_run_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    locked_by: str | None = Field(default=None, max_length=120)
    locked_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    error_code: str | None = Field(default=None, max_length=80)
    error_class: str | None = Field(default=None, max_length=80)
    provider_outage: bool = False
    retry_history_json: list[dict[str, object]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    delayed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    timed_out_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_attempt_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_by_id: uuid.UUID = Field(foreign_key="users.id")
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class JobAttempt(TenantRow, table=True):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "job_id", "attempt_no", name="uq_job_attempt_number"
        ),
        Index("ix_job_attempt_job", "clinic_id", "job_id"),
        Index("ix_job_attempt_worker", "clinic_id", "worker_membership_id"),
        ForeignKeyConstraint(
            ["clinic_id", "job_id"],
            ["jobs.clinic_id", "jobs.id"],
            name="fk_job_attempt_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "worker_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_job_attempt_worker_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    job_id: uuid.UUID
    worker_membership_id: uuid.UUID | None = None
    attempt_no: int = Field(ge=1)
    status: str = Field(default="started", max_length=30)
    error_code: str | None = Field(default=None, max_length=80)
    error_class: str | None = Field(default=None, max_length=80)
    retry_scheduled_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    duration_ms: int | None = Field(default=None, ge=0)
    started_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class RedactionRun(TenantRow, table=True):
    __tablename__ = "redaction_runs"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_redaction_run_clinic_id"),
        Index("ix_redaction_source", "clinic_id", "source_entry_version_id"),
        ForeignKeyConstraint(
            ["clinic_id", "source_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_redaction_source_version_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_entry_version_id: uuid.UUID
    status: str = Field(max_length=30)
    pipeline_version: str = Field(default="nightingale-redaction-v1", max_length=80)
    input_sha256: str = Field(max_length=64)
    redacted_sha256: str = Field(max_length=64)
    entity_counts_json: dict[str, int] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    map_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    residual_scan_passed: bool = False
    error_code: str | None = Field(default=None, max_length=80)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AIRun(TenantRow, table=True):
    __tablename__ = "ai_runs"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_ai_run_clinic_id"),
        UniqueConstraint("clinic_id", "job_id", name="uq_ai_run_job"),
        Index("ix_ai_run_patient_created", "clinic_id", "patient_id", "created_at"),
        Index("ix_ai_run_worker", "clinic_id", "executed_by_worker_membership_id"),
        CheckConstraint(
            "interaction_type IN ('care_note', 'doctor_consult', "
            "'patient_insight', 'voice_session')",
            name="ck_ai_run_interaction_type",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_ai_run_patient_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "job_id"],
            ["jobs.clinic_id", "jobs.id"],
            name="fk_ai_run_job_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "redaction_run_id"],
            ["redaction_runs.clinic_id", "redaction_runs.id"],
            name="fk_ai_run_redaction_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "source_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_ai_run_source_version_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "output_entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_ai_run_output_entry_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "output_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_ai_run_output_version_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "executed_by_worker_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_ai_run_worker_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    job_id: uuid.UUID
    redaction_run_id: uuid.UUID
    source_entry_version_id: uuid.UUID
    executed_by_worker_membership_id: uuid.UUID | None = None
    interaction_type: str = Field(max_length=60)
    provider: str = Field(max_length=60)
    model: str = Field(max_length=160)
    review_model: str | None = Field(default=None, max_length=160)
    review_status: str = Field(default="not_required", max_length=30)
    primary_output_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    review_output_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    status: str = Field(max_length=30)
    risk_tier: str = Field(default="standard", max_length=30)
    fallback_reason: str | None = Field(default=None, max_length=100)
    needs_review: bool = False
    request_sha256: str = Field(max_length=64)
    output_entry_id: uuid.UUID | None = None
    output_entry_version_id: uuid.UUID | None = None
    warnings_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    stale_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImportanceFeedbackEvent(TenantRow, table=True):
    __tablename__ = "importance_feedback_events"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "idempotency_key", name="uq_importance_feedback_idempotency"
        ),
        Index("ix_importance_feedback_highlight", "clinic_id", "highlight_id"),
        ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_importance_feedback_highlight_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "actor_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_feedback_actor_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    highlight_id: uuid.UUID
    actor_membership_id: uuid.UUID
    signal: str = Field(max_length=30)
    reason: str | None = Field(default=None, max_length=40)
    feature_keys_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    applied_delta: float = Field(sa_column=Column(Float, nullable=False))
    idempotency_key: str = Field(max_length=200)
    request_sha256: str = Field(max_length=64)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImportanceFeatureStat(TenantRow, table=True):
    __tablename__ = "importance_feature_stats"
    __table_args__ = (
        UniqueConstraint("clinic_id", "feature_key", name="uq_importance_feature_stat"),
        Index("ix_importance_feature_clinic", "clinic_id", "feature_key"),
        CheckConstraint(
            "weight >= -0.20 AND weight <= 0.20",
            name="ck_importance_feature_weight_bound",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    feature_key: str = Field(max_length=120)
    weight: float = Field(default=0.0, sa_column=Column(Float, nullable=False))
    positive_count: int = 0
    negative_count: int = 0
    observation_count: int = 0
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImportanceImpression(TenantRow, table=True):
    __tablename__ = "importance_impressions"
    __table_args__ = (
        CheckConstraint("rank >= 1", name="ck_importance_impression_rank"),
        UniqueConstraint(
            "clinic_id", "view_event_id", name="uq_importance_impression_view_event"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_importance_impression_highlight",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "viewer_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_impression_viewer",
        ),
        Index("ix_importance_impression_shown", "clinic_id", "shown_at"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    highlight_id: uuid.UUID
    viewer_membership_id: uuid.UUID
    # The safety-review surface is intentionally uncapped, so its truthful
    # impression rank may be greater than five.
    rank: int = Field(ge=1)
    surface: str = Field(default="current_priorities", max_length=60)
    view_event_id: str = Field(
        default_factory=lambda: f"server:{uuid.uuid4()}", max_length=120
    )
    exposure_probability: float = Field(
        default=1.0, sa_column=Column(Float, nullable=False)
    )
    visible_ratio: float = Field(default=0.5, sa_column=Column(Float, nullable=False))
    visible_duration_ms: int = Field(default=2_000, ge=2_000)
    shown_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImportanceCandidateExposure(TenantRow, table=True):
    """Complete candidate-set telemetry, including candidates not displayed."""

    __tablename__ = "importance_candidate_exposures"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id",
            "candidate_set_id",
            "highlight_id",
            name="uq_importance_candidate_exposure",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_importance_candidate_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_importance_candidate_highlight",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "viewer_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_candidate_viewer",
        ),
        Index(
            "ix_importance_candidate_observed",
            "clinic_id",
            "patient_id",
            "observed_at",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    highlight_id: uuid.UUID
    viewer_membership_id: uuid.UUID
    view_event_id: str = Field(max_length=120)
    candidate_set_id: str = Field(max_length=120)
    rank: int = Field(ge=1)
    surface: str = Field(default="current_priorities", max_length=60)
    feature_keys_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    shadow_score: float = Field(default=0.0, sa_column=Column(Float, nullable=False))
    protected: bool = False
    displayed: bool = False
    exposure_probability: float = Field(
        default=0.0, sa_column=Column(Float, nullable=False)
    )
    observed_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImportanceCandidateSet(TenantRow, table=True):
    """Declared per-surface totals used to audit candidate telemetry recall."""

    __tablename__ = "importance_candidate_sets"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "candidate_set_id", name="uq_importance_candidate_set"
        ),
        CheckConstraint(
            "total_candidate_count >= 0 "
            "AND current_priorities_candidate_count >= 0 "
            "AND clinical_review_candidate_count >= 0 "
            "AND current_priorities_protected_candidate_count >= 0 "
            "AND current_priorities_ordinary_candidate_count >= 0 "
            "AND clinical_review_protected_candidate_count >= 0 "
            "AND clinical_review_ordinary_candidate_count >= 0 "
            "AND protected_candidate_count >= 0 "
            "AND ordinary_candidate_count >= 0 "
            "AND current_priorities_displayed_count >= 0 "
            "AND clinical_review_displayed_count >= 0 "
            "AND displayed_count >= 0",
            name="ck_importance_candidate_set_counts_nonnegative",
        ),
        CheckConstraint(
            "total_candidate_count = current_priorities_candidate_count "
            "+ clinical_review_candidate_count "
            "AND total_candidate_count = protected_candidate_count "
            "+ ordinary_candidate_count "
            "AND current_priorities_candidate_count = "
            "current_priorities_protected_candidate_count "
            "+ current_priorities_ordinary_candidate_count "
            "AND clinical_review_candidate_count = "
            "clinical_review_protected_candidate_count "
            "+ clinical_review_ordinary_candidate_count "
            "AND protected_candidate_count = "
            "current_priorities_protected_candidate_count "
            "+ clinical_review_protected_candidate_count "
            "AND ordinary_candidate_count = "
            "current_priorities_ordinary_candidate_count "
            "+ clinical_review_ordinary_candidate_count "
            "AND displayed_count = current_priorities_displayed_count "
            "+ clinical_review_displayed_count "
            "AND current_priorities_displayed_count "
            "<= current_priorities_candidate_count "
            "AND clinical_review_displayed_count "
            "<= clinical_review_candidate_count",
            name="ck_importance_candidate_set_count_totals",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_importance_candidate_set_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "viewer_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_candidate_set_viewer",
        ),
        Index(
            "ix_importance_candidate_set_observed",
            "clinic_id",
            "observed_at",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    viewer_membership_id: uuid.UUID
    candidate_set_id: str = Field(max_length=120)
    total_candidate_count: int = Field(ge=0)
    current_priorities_candidate_count: int = Field(ge=0)
    clinical_review_candidate_count: int = Field(ge=0)
    current_priorities_protected_candidate_count: int = Field(ge=0)
    current_priorities_ordinary_candidate_count: int = Field(ge=0)
    clinical_review_protected_candidate_count: int = Field(ge=0)
    clinical_review_ordinary_candidate_count: int = Field(ge=0)
    protected_candidate_count: int = Field(ge=0)
    ordinary_candidate_count: int = Field(ge=0)
    current_priorities_displayed_count: int = Field(ge=0)
    clinical_review_displayed_count: int = Field(ge=0)
    displayed_count: int = Field(ge=0)
    observed_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ImportanceExposureQualificationReport(TenantRow, table=True):
    """Persisted recall audit that gates active importance learning."""

    __tablename__ = "importance_exposure_qualification_reports"
    __table_args__ = (
        CheckConstraint(
            "window_end >= window_start AND expires_at > created_at",
            name="ck_importance_exposure_report_window",
        ),
        CheckConstraint(
            "source_candidate_set_count >= 0 "
            "AND candidate_count >= 0 "
            "AND telemetry_count >= 0 "
            "AND displayed_count >= 0 "
            "AND protected_candidate_count >= 0 "
            "AND protected_displayed_count >= 0 "
            "AND ordinary_candidate_count >= 0 "
            "AND ordinary_displayed_count >= 0 "
            "AND missing_telemetry_count >= 0 "
            "AND duplicate_telemetry_count >= 0",
            name="ck_importance_exposure_report_counts_nonnegative",
        ),
        CheckConstraint(
            "protected_recall >= 0 AND protected_recall <= 1 "
            "AND ordinary_recall >= 0 AND ordinary_recall <= 1 "
            "AND ordinary_exposure_rate >= 0 AND ordinary_exposure_rate <= 1",
            name="ck_importance_exposure_report_rates",
        ),
        CheckConstraint(
            "NOT qualified OR (missing_telemetry_count = 0 "
            "AND duplicate_telemetry_count = 0 "
            "AND protected_candidate_count > 0 "
            "AND ordinary_candidate_count > 0 "
            "AND protected_recall = 1 "
            "AND ordinary_recall = 1 "
            "AND jsonb_array_length(qualification_reasons_json) = 0)",
            name="ck_importance_exposure_report_qualified",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "generated_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_exposure_report_generator",
        ),
        Index(
            "ix_importance_exposure_report_current",
            "clinic_id",
            "created_at",
            "expires_at",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    report_version: str = Field(max_length=80)
    window_start: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    window_end: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    source_candidate_set_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    telemetry_count: int = Field(ge=0)
    displayed_count: int = Field(ge=0)
    protected_candidate_count: int = Field(ge=0)
    protected_displayed_count: int = Field(ge=0)
    ordinary_candidate_count: int = Field(ge=0)
    ordinary_displayed_count: int = Field(ge=0)
    protected_recall: float = Field(sa_column=Column(Float, nullable=False))
    ordinary_recall: float = Field(sa_column=Column(Float, nullable=False))
    ordinary_exposure_rate: float = Field(sa_column=Column(Float, nullable=False))
    missing_telemetry_count: int = Field(ge=0)
    duplicate_telemetry_count: int = Field(ge=0)
    surface_metrics_json: dict[str, dict[str, int | float]] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    qualified: bool = False
    qualification_reasons_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    generated_by_membership_id: uuid.UUID
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class DecisionAssessment(TenantRow, table=True):
    __tablename__ = "decision_assessments"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_decision_assessment_clinic_id"),
        UniqueConstraint(
            "clinic_id", "highlight_id", name="uq_decision_assessment_highlight"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_decision_assessment_highlight",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "assertion_id"],
            ["clinical_fact_assertions.clinic_id", "clinical_fact_assertions.id"],
            name="fk_decision_assessment_assertion",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "calibration_report_id"],
            ["calibration_reports.clinic_id", "calibration_reports.id"],
            name="fk_decision_assessment_calibration",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    highlight_id: uuid.UUID
    assertion_id: uuid.UUID | None = Field(default=None)
    output_type: str = Field(default="extracted_fact", max_length=40)
    support_state: str = Field(default="supported", max_length=30)
    risk_tier: str = Field(default="standard", max_length=20)
    deterministic_floor: str = Field(default="standard", max_length=20)
    model_risk: str | None = Field(default=None, max_length=20)
    effective_risk: str = Field(default="standard", max_length=20)
    risk_rule_version: str = Field(default="clinical-risk-rules-v2", max_length=60)
    risk_rule_ids_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    confidence_value: float | None = Field(default=None)
    confidence_band: str = Field(default="unavailable", max_length=20)
    confidence_lower_bound: float | None = Field(default=None)
    calibration_report_id: uuid.UUID | None = Field(default=None)
    calibration_version: str | None = Field(default=None, max_length=80)
    abstained: bool = False
    abstention_reason: str | None = Field(default=None, max_length=80)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ClinicalFactAssertion(TenantRow, table=True):
    """Canonical, source-bound fact used by risk, conflict, and sharing gates."""

    __tablename__ = "clinical_fact_assertions"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_fact_assertion_clinic_id"),
        CheckConstraint(
            "assertion_scope IN ('specific_substance','drug_allergies','all_allergies')",
            name="ck_fact_assertion_scope",
        ),
        CheckConstraint(
            "assertion_state IN ('active','superseded')",
            name="ck_fact_assertion_state",
        ),
        CheckConstraint(
            "allergy_category IS NULL OR "
            "(fact_type = 'allergy' AND "
            "allergy_category IN ('drug','food','environmental'))",
            name="ck_fact_assertion_allergy_category",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_fact_assertion_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_fact_assertion_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "source_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_fact_assertion_version",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "provenance_pointer_id"],
            ["provenance_pointers.clinic_id", "provenance_pointers.id"],
            name="fk_fact_assertion_pointer",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "superseded_by_assertion_id"],
            ["clinical_fact_assertions.clinic_id", "clinical_fact_assertions.id"],
            name="fk_fact_assertion_superseded_by",
        ),
        CheckConstraint(
            "superseded_by_assertion_id IS NULL OR superseded_by_assertion_id <> id",
            name="ck_fact_assertion_not_self_superseding",
        ),
        Index(
            "ix_fact_assertion_patient_type",
            "clinic_id",
            "patient_id",
            "fact_type",
        ),
        Index(
            "ix_clinicalfactassertion_normalized_key_hash",
            "normalized_key_hash",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    source_entry_version_id: uuid.UUID
    provenance_pointer_id: uuid.UUID
    highlight_id: uuid.UUID | None = Field(default=None)
    fact_type: str = Field(max_length=80)
    subject_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    normalized_value_ciphertext: bytes = Field(
        sa_column=Column(LargeBinary, nullable=False)
    )
    normalized_key_hash: str = Field(max_length=64)
    polarity: str = Field(default="present", max_length=20)
    assertion_scope: str = Field(default="specific_substance", max_length=40)
    allergy_category: str | None = Field(default=None, max_length=20)
    source_language: str = Field(default="und", max_length=20)
    source_role: str | None = Field(default=None, max_length=20)
    source_section: str | None = Field(default=None, max_length=20)
    assertion_state: str = Field(default="active", max_length=20)
    superseded_by_assertion_id: uuid.UUID | None = None
    superseded_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    clinical_status: str = Field(default="active", max_length=30)
    effective_time: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    medication_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    dose_value: float | None = Field(default=None)
    dose_unit: str | None = Field(default=None, max_length=20)
    route: str | None = Field(default=None, max_length=40)
    frequency: str | None = Field(default=None, max_length=40)
    origin: str = Field(max_length=20)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ProvisionalSafetyAlert(TenantRow, table=True):
    """A deduplicated live finding that cannot become a fact without review."""

    __tablename__ = "provisional_safety_alerts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','confirmed','dismissed','superseded')",
            name="ck_provisional_safety_alert_state",
        ),
        CheckConstraint(
            "source_start_offset >= 0 AND source_end_offset >= source_start_offset",
            name="ck_provisional_safety_alert_span",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_provisional_alert_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id"],
            ["voice_sessions.clinic_id", "voice_sessions.id"],
            name="fk_provisional_alert_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "reviewed_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_provisional_alert_reviewer",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "confirmed_assertion_id"],
            ["clinical_fact_assertions.clinic_id", "clinical_fact_assertions.id"],
            name="fk_provisional_alert_assertion",
        ),
        UniqueConstraint(
            "clinic_id",
            "session_id",
            "deduplication_key",
            name="uq_provisional_alert_dedup",
        ),
        Index(
            "ix_provisional_alert_pending",
            "clinic_id",
            "patient_id",
            "state",
            "detected_at",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    session_id: uuid.UUID
    source_event_id: str = Field(max_length=160)
    source_text_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    source_text_sha256: str = Field(max_length=64)
    source_start_offset: int = Field(ge=0)
    source_end_offset: int = Field(ge=0)
    source_language: str = Field(default="und", max_length=20)
    concept_code: str = Field(max_length=120)
    assertion_scope: str = Field(default="specific_substance", max_length=40)
    polarity: str = Field(default="present", max_length=20)
    deduplication_key: str = Field(max_length=64)
    severity: str = Field(default="critical", max_length=20)
    state: str = Field(default="pending", max_length=20)
    completed_segment_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    detected_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    reviewed_by_membership_id: uuid.UUID | None = None
    reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    review_reason_code: str | None = Field(default=None, max_length=80)
    confirmed_assertion_id: uuid.UUID | None = None


class EvaluationRun(TenantRow, table=True):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_evaluation_run_clinic_id"),
        Index("ix_evaluation_run_task_created", "clinic_id", "task", "created_at"),
        CheckConstraint(
            "total_sample_count >= 0 AND calibration_sample_count >= 0 AND "
            "holdout_sample_count >= 0 AND sample_count = holdout_sample_count AND "
            "total_sample_count = calibration_sample_count + holdout_sample_count",
            name="ck_evaluation_run_sample_accounting",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    provider: str = Field(max_length=80)
    exact_model_id: str = Field(max_length=160)
    task: str = Field(max_length=80)
    request_parameters_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    dataset_manifest_sha256: str = Field(max_length=64)
    code_commit: str = Field(max_length=64)
    calibration_split: str = Field(max_length=100)
    holdout_split: str = Field(max_length=100)
    total_sample_count: int = Field(default=0, ge=0)
    calibration_sample_count: int = Field(default=0, ge=0)
    holdout_sample_count: int = Field(default=0, ge=0)
    # Compatibility projection for older readers. It is database-bound to the
    # untouched holdout population rather than the total evaluation population.
    sample_count: int = Field(ge=0)
    status: str = Field(default="pending", max_length=30)
    metrics_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class CalibrationReport(TenantRow, table=True):
    __tablename__ = "calibration_reports"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_calibration_report_clinic_id"),
        Index(
            "ix_calibration_report_lookup",
            "clinic_id",
            "provider",
            "exact_model_id",
            "task",
            "expires_at",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "evaluation_run_id"],
            ["evaluation_runs.clinic_id", "evaluation_runs.id"],
            name="fk_calibration_report_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "total_sample_count >= 0 AND calibration_sample_count >= 0 AND "
            "holdout_sample_count >= 0 AND sample_count = holdout_sample_count AND "
            "total_sample_count = calibration_sample_count + holdout_sample_count",
            name="ck_calibration_report_sample_accounting",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    evaluation_run_id: uuid.UUID
    provider: str = Field(max_length=80)
    exact_model_id: str = Field(max_length=160)
    task: str = Field(max_length=80)
    request_parameters_sha256: str = Field(max_length=64)
    dataset_manifest_sha256: str = Field(max_length=64)
    code_commit: str = Field(max_length=64)
    total_sample_count: int = Field(default=0, ge=0)
    calibration_sample_count: int = Field(default=0, ge=0)
    holdout_sample_count: int = Field(default=0, ge=0)
    # Compatibility projection retained for generated artifacts and older
    # integrations; the database constrains it to ``holdout_sample_count``.
    sample_count: int = Field(ge=0)
    consultation_count: int = Field(ge=0)
    confidence_band: str = Field(default="unavailable", max_length=20)
    accuracy_lower_bound: float | None = Field(default=None)
    metrics_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class CalibrationBucket(TenantRow, table=True):
    __tablename__ = "calibration_buckets"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id",
            "calibration_report_id",
            "bucket_key",
            name="uq_calibration_bucket",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "calibration_report_id"],
            ["calibration_reports.clinic_id", "calibration_reports.id"],
            name="fk_calibration_bucket_report",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    calibration_report_id: uuid.UUID
    bucket_key: str = Field(max_length=120)
    sample_count: int = Field(ge=0)
    consultation_count: int = Field(ge=0)
    estimated_accuracy: float | None = Field(default=None)
    accuracy_lower_bound: float | None = Field(default=None)
    confidence_band: str = Field(default="unavailable", max_length=20)
    metrics_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )


class RedactionEvaluationRun(TenantRow, table=True):
    __tablename__ = "redaction_evaluation_runs"
    __table_args__ = (
        Index(
            "ix_redaction_eval_version", "clinic_id", "redactor_version", "created_at"
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    redactor_version: str = Field(max_length=80)
    dataset_sha256: str = Field(max_length=64)
    sample_count: int = Field(ge=0)
    phi_recall: float = Field(default=0.0)
    residual_phi_count: int = Field(default=0, ge=0)
    clinical_span_damage_count: int = Field(default=0, ge=0)
    passed: bool = False
    metrics_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientSharingRequest(TenantRow, table=True):
    __tablename__ = "patient_sharing_requests"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "id", name="uq_patient_sharing_request_clinic_id"
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','superseded','withdrawn')",
            name="ck_patient_sharing_request_status",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_sharing_request_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id", "entry_id"],
            ["entries.clinic_id", "entries.patient_id", "entries.id"],
            name="fk_patient_sharing_request_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "entry_id", "entry_version_id"],
            [
                "entry_versions.clinic_id",
                "entry_versions.entry_id",
                "entry_versions.id",
            ],
            name="fk_patient_sharing_request_version",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "requested_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_sharing_request_membership",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "reviewed_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_sharing_request_reviewer",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id", "entry_id", "publication_id"],
            [
                "patient_publications.clinic_id",
                "patient_publications.patient_id",
                "patient_publications.entry_id",
                "patient_publications.id",
            ],
            name="fk_patient_sharing_request_publication",
            use_alter=True,
        ),
        Index(
            "uq_patient_sharing_request_pending_entry",
            "clinic_id",
            "entry_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_patient_sharing_request_status",
            "clinic_id",
            "patient_id",
            "status",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    requested_by_membership_id: uuid.UUID
    status: str = Field(default="pending", max_length=20)
    reviewed_by_membership_id: uuid.UUID | None = Field(default=None)
    publication_id: uuid.UUID | None = Field(default=None)
    reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PatientPublication(TenantRow, table=True):
    __tablename__ = "patient_publications"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_patient_publication_clinic_id"),
        UniqueConstraint(
            "clinic_id",
            "patient_id",
            "entry_id",
            "id",
            name="uq_patient_publication_scope_id",
        ),
        CheckConstraint(
            "supersedes_publication_id IS NULL OR supersedes_publication_id <> id",
            name="ck_patient_publication_not_self_superseding",
        ),
        CheckConstraint(
            "((correction_idempotency_key_sha256 IS NULL) = "
            "(correction_request_sha256 IS NULL)) AND "
            "(correction_idempotency_key_sha256 IS NULL OR "
            "(supersedes_publication_id IS NOT NULL AND "
            "correction_idempotency_key_sha256 ~ '^[0-9a-f]{64}$' AND "
            "correction_request_sha256 ~ '^[0-9a-f]{64}$'))",
            name="ck_patient_publication_correction_hashes",
        ),
        UniqueConstraint(
            "clinic_id",
            "correction_idempotency_key_sha256",
            name="uq_patient_publication_correction_idempotency",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_publication_patient",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id", "entry_id"],
            ["entries.clinic_id", "entries.patient_id", "entries.id"],
            name="fk_patient_publication_entry",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "entry_id", "entry_version_id"],
            [
                "entry_versions.clinic_id",
                "entry_versions.entry_id",
                "entry_versions.id",
            ],
            name="fk_patient_publication_version",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "approved_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_publication_approver",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "medication_reviewed_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_publication_medication_reviewer",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "withdrawn_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_publication_withdrawer",
        ),
        ForeignKeyConstraint(
            [
                "clinic_id",
                "patient_id",
                "entry_id",
                "supersedes_publication_id",
            ],
            [
                "patient_publications.clinic_id",
                "patient_publications.patient_id",
                "patient_publications.entry_id",
                "patient_publications.id",
            ],
            name="fk_patient_publication_supersedes",
        ),
        Index(
            "uq_patient_publication_active_entry",
            "clinic_id",
            "entry_id",
            unique=True,
            postgresql_where=text("withdrawn_at IS NULL"),
        ),
        Index(
            "ix_patient_publication_active",
            "clinic_id",
            "patient_id",
            "withdrawn_at",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    supersedes_publication_id: uuid.UUID | None = Field(default=None)
    approved_by_membership_id: uuid.UUID
    approval_policy_version: str = Field(default="patient-sharing-v1", max_length=80)
    medication_review_complete: bool = False
    medication_review_json: list[dict[str, object]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    medication_reviewed_by_membership_id: uuid.UUID | None = None
    medication_reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    correction_reason_code: str | None = Field(default=None, max_length=80)
    correction_idempotency_key_sha256: str | None = Field(default=None, max_length=64)
    correction_request_sha256: str | None = Field(default=None, max_length=64)
    withdrawn_by_membership_id: uuid.UUID | None = None
    approved_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    withdrawn_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class PatientPublicationItem(TenantRow, table=True):
    __tablename__ = "patient_publication_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["clinic_id", "publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_patient_publication_item_publication",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "provenance_pointer_id"],
            ["provenance_pointers.clinic_id", "provenance_pointers.id"],
            name="fk_patient_publication_item_pointer",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "assertion_id"],
            ["clinical_fact_assertions.clinic_id", "clinical_fact_assertions.id"],
            name="fk_publication_item_assertion",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "decision_assessment_id"],
            ["decision_assessments.clinic_id", "decision_assessments.id"],
            name="fk_publication_item_assessment",
        ),
        UniqueConstraint(
            "clinic_id",
            "publication_id",
            "provenance_pointer_id",
            name="uq_patient_publication_item_pointer",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    publication_id: uuid.UUID
    assertion_id: uuid.UUID | None = Field(default=None)
    provenance_pointer_id: uuid.UUID
    decision_assessment_id: uuid.UUID | None = Field(default=None)
    support_state: str = Field(max_length=30)
    confidence_band: str = Field(max_length=20)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ArchiveBlob(TenantRow, table=True):
    __tablename__ = "archive_blobs"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_archive_blob_clinic_id"),
        UniqueConstraint(
            "clinic_id", "entry_version_id", name="uq_archive_entry_version"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_archive_entry_version_tenant",
            use_alter=True,
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entry_version_id: uuid.UUID
    compression: str = Field(default="zstd", max_length=20)
    encryption: str = Field(default="aes-256-gcm", max_length=30)
    key_id: str = Field(default="field-master-v1", max_length=80)
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    plaintext_sha256: str = Field(max_length=64)
    ciphertext_sha256: str = Field(max_length=64)
    original_size: int
    compressed_size: int
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class DecayRun(TenantRow, table=True):
    __tablename__ = "decay_runs"
    __table_args__ = (Index("ix_decay_run_clinic_created", "clinic_id", "created_at"),)
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    policy_version: str = Field(default="nightingale-decay-v1", max_length=80)
    cutoff_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    dry_run: bool = True
    candidate_count: int = 0
    archived_count: int = 0
    error_count: int = 0
    created_by_id: uuid.UUID = Field(foreign_key="users.id")
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class RetentionLock(TenantRow, table=True):
    __tablename__ = "retention_locks"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id", "entity_type", "entity_id", name="uq_retention_lock_entity"
        ),
        Index("ix_retention_lock_entity", "clinic_id", "entity_type", "entity_id"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entity_type: str = Field(max_length=40)
    entity_id: uuid.UUID
    reason_code: str = Field(max_length=80)
    locked_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_by_id: uuid.UUID = Field(foreign_key="users.id")
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class VoiceSession(TenantRow, table=True):
    __tablename__ = "voice_sessions"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_voice_session_clinic_id"),
        Index(
            "ix_voice_session_patient_created", "clinic_id", "patient_id", "created_at"
        ),
        Index("ix_voice_session_state_updated", "clinic_id", "state", "updated_at"),
        CheckConstraint(
            "state IN ('created','recording','finalizing','assembling',"
            "'preprocessing','transcribing','redacting','extracting',"
            "'ready','needs_review','published')",
            name="ck_voice_session_state",
        ),
        CheckConstraint(
            "live_transcript_status IN "
            "('not_started','available','unavailable','needs_review','replaced')",
            name="ck_voice_session_live_transcript_status",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_voice_session_patient_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "id", "current_transcript_revision_id"],
            [
                "transcript_revisions.clinic_id",
                "transcript_revisions.session_id",
                "transcript_revisions.id",
            ],
            name="fk_voice_session_current_revision_tenant",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "processing_job_id"],
            ["jobs.clinic_id", "jobs.id"],
            name="fk_voice_session_job_tenant",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["clinic_id", "published_entry_id"],
            ["entries.clinic_id", "entries.id"],
            name="fk_voice_session_published_entry_tenant",
            use_alter=True,
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    capture_kind: str = Field(max_length=20)
    state: str = Field(default="created", max_length=30)
    synthetic_fixture: bool = False
    fixture_id: str | None = Field(default=None, max_length=100)
    created_by_id: uuid.UUID = Field(foreign_key="users.id")
    current_transcript_revision_id: uuid.UUID | None = None
    processing_job_id: uuid.UUID | None = None
    published_entry_id: uuid.UUID | None = None
    patient_summary_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    warning_codes_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    error_code: str | None = Field(default=None, max_length=80)
    live_transcript_status: str = Field(default="not_started", max_length=30)
    live_transcript_error_code: str | None = Field(default=None, max_length=80)
    remote_audio_consent_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    remote_audio_consent_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id"
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class VoiceDevice(TenantRow, table=True):
    __tablename__ = "voice_devices"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_voice_device_clinic_id"),
        UniqueConstraint(
            "clinic_id", "session_id", "id", name="uq_voice_device_session_id"
        ),
        UniqueConstraint(
            "clinic_id",
            "session_id",
            "client_device_id",
            name="uq_voice_device_client_id",
        ),
        Index("ix_voice_device_session", "clinic_id", "session_id"),
        CheckConstraint(
            "last_declared_chunk_index IS NULL OR "
            "last_declared_chunk_index BETWEEN 0 AND 21600",
            name="ck_voice_device_last_index",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id"],
            ["voice_sessions.clinic_id", "voice_sessions.id"],
            name="fk_voice_device_session_tenant",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID
    client_device_id: str = Field(max_length=120)
    capture_role: str = Field(max_length=30)
    joined_by_id: uuid.UUID = Field(foreign_key="users.id")
    last_declared_chunk_index: int | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AudioChunk(TenantRow, table=True):
    __tablename__ = "audio_chunks"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_audio_chunk_clinic_id"),
        UniqueConstraint(
            "clinic_id", "device_id", "chunk_index", name="uq_audio_chunk_index"
        ),
        Index(
            "ix_audio_chunk_session_device_index",
            "clinic_id",
            "session_id",
            "device_id",
            "chunk_index",
        ),
        CheckConstraint(
            "chunk_index BETWEEN 0 AND 21600", name="ck_audio_chunk_index_bound"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id"],
            ["voice_sessions.clinic_id", "voice_sessions.id"],
            name="fk_audio_chunk_session_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id", "device_id"],
            ["voice_devices.clinic_id", "voice_devices.session_id", "voice_devices.id"],
            name="fk_audio_chunk_device_tenant",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID
    device_id: uuid.UUID
    chunk_index: int = Field(ge=0)
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    plaintext_sha256: str = Field(max_length=64)
    byte_length: int = Field(ge=1)
    media_type: str = Field(max_length=100)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    received_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AudioAsset(TenantRow, table=True):
    __tablename__ = "audio_assets"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_audio_asset_clinic_id"),
        UniqueConstraint("clinic_id", "session_id", name="uq_audio_asset_session"),
        UniqueConstraint(
            "clinic_id", "session_id", "id", name="uq_audio_asset_session_id"
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id"],
            ["voice_sessions.clinic_id", "voice_sessions.id"],
            name="fk_audio_asset_session_tenant",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    plaintext_sha256: str = Field(max_length=64)
    duration_ms: int = Field(ge=0)
    media_type: str = Field(default="audio/wav", max_length=100)
    sample_rate_hz: int = Field(default=16_000, ge=1)
    channels: int = Field(default=1, ge=1)
    preprocessing_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class TranscriptRevision(TenantRow, table=True):
    __tablename__ = "transcript_revisions"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_transcript_revision_clinic_id"),
        UniqueConstraint(
            "clinic_id",
            "session_id",
            "id",
            name="uq_transcript_revision_session_id",
        ),
        UniqueConstraint(
            "clinic_id",
            "session_id",
            "revision_no",
            name="uq_transcript_revision_number",
        ),
        Index(
            "ix_transcript_revision_session_created",
            "clinic_id",
            "session_id",
            "created_at",
        ),
        CheckConstraint(
            "status IN ('ready','needs_review')",
            name="ck_transcript_revision_status",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id"],
            ["voice_sessions.clinic_id", "voice_sessions.id"],
            name="fk_transcript_revision_session_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id", "previous_revision_id"],
            [
                "transcript_revisions.clinic_id",
                "transcript_revisions.session_id",
                "transcript_revisions.id",
            ],
            name="fk_transcript_previous_revision_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID
    revision_no: int = Field(ge=1)
    previous_revision_id: uuid.UUID | None = None
    text_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    text_sha256: str = Field(max_length=64)
    summary_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    provider: str = Field(max_length=80)
    model: str = Field(max_length=160)
    detected_language: str | None = Field(default=None, max_length=80)
    status: str = Field(default="ready", max_length=30)
    needs_review: bool = False
    stale: bool = False
    fallback: bool = False
    corrected_by_id: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    warning_codes_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class TranscriptSegment(TenantRow, table=True):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_transcript_segment_clinic_id"),
        UniqueConstraint(
            "clinic_id",
            "session_id",
            "revision_id",
            "id",
            name="uq_transcript_segment_revision_id",
        ),
        UniqueConstraint(
            "clinic_id", "revision_id", "ordinal", name="uq_transcript_segment_ordinal"
        ),
        Index(
            "ix_transcript_segment_revision_time",
            "clinic_id",
            "revision_id",
            "start_ms",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id", "revision_id"],
            [
                "transcript_revisions.clinic_id",
                "transcript_revisions.session_id",
                "transcript_revisions.id",
            ],
            name="fk_transcript_segment_revision_tenant",
            ondelete="CASCADE",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID
    revision_id: uuid.UUID
    ordinal: int = Field(ge=0)
    text_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    text_sha256: str = Field(max_length=64)
    text_start: int = Field(ge=0)
    text_end: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker_id: str | None = Field(default=None, max_length=80)
    speaker_ids_json: list[str] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    detected_language: str | None = Field(default=None, max_length=80)
    source_language: str = Field(default="und", max_length=20)
    language_confidence: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )
    language_spans_json: list[dict[str, object]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    confidence: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True)
    )
    confidence_source: str = Field(max_length=80)
    overlap_group_id: str | None = Field(default=None, max_length=100)
    provider: str = Field(max_length=80)
    model: str = Field(max_length=160)


class ClinicalFact(TenantRow, table=True):
    __tablename__ = "clinical_facts"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_clinical_fact_clinic_id"),
        UniqueConstraint(
            "clinic_id", "revision_id", "ordinal", name="uq_clinical_fact_ordinal"
        ),
        Index("ix_clinical_fact_revision_status", "clinic_id", "revision_id", "status"),
        CheckConstraint(
            "status IN ('proposed','accepted','rejected')",
            name="ck_clinical_fact_status",
        ),
        CheckConstraint(
            "(status = 'proposed' AND reviewed_by_id IS NULL AND reviewed_at IS NULL) "
            "OR (status IN ('accepted','rejected') AND reviewed_by_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_clinical_fact_review_pair",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id", "revision_id"],
            [
                "transcript_revisions.clinic_id",
                "transcript_revisions.session_id",
                "transcript_revisions.id",
            ],
            name="fk_clinical_fact_revision_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id", "revision_id", "segment_id"],
            [
                "transcript_segments.clinic_id",
                "transcript_segments.session_id",
                "transcript_segments.revision_id",
                "transcript_segments.id",
            ],
            name="fk_clinical_fact_segment_tenant",
        ),
        ForeignKeyConstraint(
            ["clinic_id", "session_id", "audio_asset_id"],
            [
                "audio_assets.clinic_id",
                "audio_assets.session_id",
                "audio_assets.id",
            ],
            name="fk_clinical_fact_audio_asset_tenant",
        ),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID
    revision_id: uuid.UUID
    segment_id: uuid.UUID
    ordinal: int = Field(ge=0)
    fact_type: str = Field(max_length=80)
    value_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    exact_quote_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    quote_sha256: str = Field(max_length=64)
    transcript_start: int = Field(ge=0)
    transcript_end: int = Field(ge=0)
    audio_asset_id: uuid.UUID
    audio_start_ms: int = Field(ge=0)
    audio_end_ms: int = Field(ge=0)
    status: str = Field(default="proposed", max_length=30)
    patient_facing: bool = False
    stale: bool = False
    reviewed_by_id: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    reviewed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


# API schemas
class Message(SQLModel):
    message: str


class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(SQLModel):
    sub: str | None = None
    membership_id: str | None = None
    clinic_id: str | None = None
    job_id: str | None = None
    platform_admin_id: str | None = None
    scope: str | None = None


class DemoLoginRequest(SQLModel):
    persona: Literal["patient", "staff", "clinician", "admin", "worker", "other_staff"]


class MePublic(SQLModel):
    user_id: uuid.UUID
    email: EmailStr | None
    full_name: str | None
    clinic_id: uuid.UUID
    clinic_code: str
    clinic_name: str
    membership_id: uuid.UUID
    role: Role


class MembershipPublic(SQLModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: Role
    is_active: bool
    created_at: datetime


class MembershipsPublic(SQLModel):
    data: list[MembershipPublic]
    count: int


class TeamMemberPublic(SQLModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    full_name: str | None
    role: MembershipRole


class TeamMembersPublic(SQLModel):
    data: list[TeamMemberPublic]
    count: int


class MembershipCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)
    role: MembershipRole


class MembershipInvitationPublic(SQLModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: MembershipRole
    state: Literal["pending"] = "pending"
    expires_at: datetime
    created_at: datetime


class MembershipInvitationAccept(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    email: EmailStr
    token: str = Field(min_length=64, max_length=200)
    password: str = Field(min_length=16, max_length=200)
    full_name: str | None = Field(default=None, max_length=255)


class AuditEventPublic(SQLModel):
    id: uuid.UUID
    actor_id: uuid.UUID
    action: str
    resource_type: str
    resource_id: uuid.UUID
    version_id: uuid.UUID | None
    created_at: datetime
    reason_code: str = "not_specified"
    clinical_rationale_present: bool = False


class AuditEventsPublic(SQLModel):
    data: list[AuditEventPublic]
    count: int


class ClinicAISettingPublic(SQLModel):
    provider: Literal["openai"] = "openai"
    api_key_configured: bool
    api_key_last4: str | None
    credential_source: Literal["clinic", "environment", "none"]
    fast_model: str
    careful_model: str
    transcribe_model: str
    updated_at: datetime | None


class ClinicAISettingUpdate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    api_key: str | None = Field(default=None, min_length=20, max_length=500)
    clear_api_key: bool = False
    fast_model: str = Field(min_length=2, max_length=160)
    careful_model: str = Field(min_length=2, max_length=160)
    transcribe_model: str = Field(min_length=2, max_length=160)


class PatientPublic(SQLModel):
    id: uuid.UUID
    display_name: str
    date_of_birth: date | None = None
    medical_record_number: str | None = None
    same_name_count: int = 1
    today_visit_id: uuid.UUID | None = None
    today_visit_at: datetime | None = None
    today_visit_status: str | None = None
    today_visit_type: str | None = None
    last_activity_at: datetime | None = None


IdentityDocumentType = Literal["nric_fin", "passport", "other"]


class PatientIdentityInput(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    display_name: str = Field(min_length=1, max_length=255)
    date_of_birth: date
    medical_record_number: str = Field(min_length=3, max_length=80)
    identity_document_type: IdentityDocumentType
    identity_document_number: str = Field(min_length=3, max_length=80)


class PatientDuplicateCandidate(SQLModel):
    patient_id: uuid.UUID
    display_name: str
    date_of_birth: date | None
    medical_record_number: str | None
    masked_identity_document: str | None


class PatientDuplicateCheckPublic(SQLModel):
    status: Literal["clear", "possible_match", "exact_match"]
    candidates: list[PatientDuplicateCandidate]
    duplicate_confirmation_token: str | None = None


class PatientCreate(PatientIdentityInput):
    duplicate_confirmation_token: str | None = Field(default=None, max_length=2000)


class PatientDetailPublic(PatientPublic):
    date_of_birth: date | None
    medical_record_number: str | None
    identity_document_type: str | None
    masked_identity_document: str | None
    portal_access_state: Literal["not_invited", "pending", "active", "deactivated"]
    status: str


class PatientPortalInvitationCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")
    email: EmailStr


class PatientPortalInvitationPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    email: EmailStr | None
    state: Literal["pending"] = "pending"
    expires_at: datetime
    created_at: datetime
    notification_id: uuid.UUID | None = None
    notification_state: (
        Literal["queued", "submitted", "delivered", "failed", "acknowledged", "revoked"]
        | None
    ) = None


class PatientInvitationPreviewRequest(SQLModel):
    token: str = Field(min_length=64, max_length=512)
    email: EmailStr


class PatientInvitationPreviewPublic(SQLModel):
    clinic_name: str
    patient_display_name: str
    email: EmailStr
    account_exists: bool


class PatientInvitationAccept(SQLModel):
    model_config = SQLModelConfig(extra="forbid")
    token: str = Field(min_length=64, max_length=512)
    email: EmailStr
    # Existing users authenticate with their current password. New portal
    # accounts are subject to the 16-character minimum in the service layer.
    password: str = Field(min_length=1, max_length=200)
    full_name: str | None = Field(default=None, max_length=255)


class PatientAccessEnrollStartRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    invitation_token: str = Field(min_length=64, max_length=512)
    claim_code: str = Field(min_length=6, max_length=80)
    phone: str = Field(min_length=8, max_length=32)


class PatientAccessLoginStartRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    portal_id: str = Field(min_length=8, max_length=80)


class PatientOTPChallengePublic(SQLModel):
    challenge_id: uuid.UUID
    challenge_token: str
    purpose: Literal["enrollment", "login", "recovery", "phone_change"]
    portal_id: str
    masked_phone: str
    expires_at: datetime
    resend_available_at: datetime
    attempts_remaining: int
    notification_id: uuid.UUID
    delivery_state: Literal[
        "queued", "submitted", "delivered", "failed", "acknowledged", "revoked"
    ]


class PatientAccessVerifyRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    challenge_token: str = Field(min_length=32, max_length=512)
    otp: str = Field(min_length=4, max_length=10)


class PatientOTPResendRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    challenge_token: str = Field(min_length=32, max_length=512)


class PatientAccessPublic(SQLModel):
    credential_id: uuid.UUID
    patient_id: uuid.UUID
    clinic_id: uuid.UUID
    portal_id: str
    masked_phone: str
    access_state: Literal["pending", "active", "revoked"]


class PatientAccessVerifyPublic(SQLModel):
    access: PatientAccessPublic
    token: Token


# Descriptive aliases retained for early clients built from the approved plan.
PatientEnrollmentStartRequest = PatientAccessEnrollStartRequest
PatientPortalLoginStartRequest = PatientAccessLoginStartRequest
PatientOTPVerifyRequest = PatientAccessVerifyRequest


class MedicationReviewAttestation(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    assertion_id: uuid.UUID
    medication: str = Field(min_length=1, max_length=255)
    dose_value: float | None = None
    dose_unit: str | None = Field(default=None, max_length=20)
    route: str | None = Field(default=None, max_length=40)
    frequency: str | None = Field(default=None, max_length=40)
    confirmed: bool = True


MedicationReviewItem = MedicationReviewAttestation


class PatientPublicationCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    entry_version_id: uuid.UUID
    sharing_request_id: uuid.UUID | None = None
    medication_reviews: list[MedicationReviewAttestation] = Field(default_factory=list)
    correction_reason_code: str | None = Field(default=None, max_length=80)


class PatientPublicationCorrectionCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    replacement_entry_version_id: uuid.UUID
    medication_reviews: list[MedicationReviewAttestation] = Field(default_factory=list)
    # A correction changes patient-visible clinical information. Outreach is a
    # server-enforced safety invariant rather than a client preference. Keep
    # the literal field for backwards-compatible request bodies while making a
    # caller-supplied false value fail validation.
    outreach_required: Literal[True] = True


class PatientPublicationPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    supersedes_publication_id: uuid.UUID | None = None
    entry_title: str
    approved_by_name: str
    approval_policy_version: str
    approved_at: datetime
    withdrawn_at: datetime | None
    items: list[dict[str, object]] = Field(default_factory=list)
    medication_review_complete: bool = False
    medication_reviews: list[dict[str, object]] = Field(default_factory=list)
    correction_reason_code: str | None = None
    replacement_publication_id: uuid.UUID | None = None
    acknowledgement_state: Literal["not_required", "pending", "acknowledged"] = (
        "not_required"
    )
    outreach_required: bool = False
    notification_id: uuid.UUID | None = None
    notification_state: (
        Literal[
            "queued",
            "submitted",
            "delivered",
            "failed",
            "acknowledged",
            "revoked",
        ]
        | None
    ) = None
    delivery_warning: (
        Literal[
            "notification_queue_failed",
            "notification_delivery_failed",
            "notification_revoked",
        ]
        | None
    ) = None


class PatientPublicationReceiptPublic(SQLModel):
    publication_id: uuid.UUID
    entry_title: str
    approved_by_name: str
    approved_at: datetime
    withdrawn_at: datetime | None
    status: Literal["active", "withdrawn"]
    replacement_publication_id: uuid.UUID | None = None
    acknowledged_at: datetime | None = None
    outreach_status: str | None = None
    acknowledgement_state: Literal["not_required", "pending", "acknowledged"] = (
        "not_required"
    )
    outreach_required: bool = False
    replacement_entry_title: str | None = None


class PatientPublicationAcknowledgementCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    event_type: Literal["opened", "acknowledged"] = "acknowledged"
    notification_id: uuid.UUID | None = None


class PatientPublicationAcknowledgementPublic(SQLModel):
    id: uuid.UUID
    publication_id: uuid.UUID
    patient_id: uuid.UUID
    channel: str
    event_type: str
    acknowledged_at: datetime


class PublicationCorrectionOutreachPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    withdrawn_publication_id: uuid.UUID
    replacement_publication_id: uuid.UUID | None
    notification_id: uuid.UUID | None
    status: str
    due_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class PatientPortalEventPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    event_type: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    created_at: datetime


NotificationChannel = Literal["email", "sms", "whatsapp", "portal"]
NotificationState = Literal[
    "queued", "submitted", "delivered", "failed", "acknowledged", "revoked"
]


class AppointmentNotificationCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    patient_id: uuid.UUID
    visit_id: uuid.UUID
    channel: NotificationChannel
    destination: str = Field(min_length=3, max_length=320)
    scheduled_for: datetime | None = None


class NotificationResendRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    channel: NotificationChannel | None = None
    destination: str | None = Field(default=None, min_length=3, max_length=320)


class NotificationReceiptInput(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    provider_event_id: str = Field(min_length=1, max_length=200)
    provider_message_id: str = Field(min_length=1, max_length=200)
    event_type: Literal[
        "submitted",
        "delivered",
        "failed",
        "bounced",
        "undeliverable",
        "acknowledged",
    ]
    occurred_at: datetime
    payload_sha256: str = Field(min_length=64, max_length=64)


NotificationCallbackRequest = NotificationReceiptInput


class NotificationAttemptPublic(SQLModel):
    id: uuid.UUID
    notification_id: uuid.UUID
    attempt_no: int
    provider: str
    provider_message_id: str | None
    status: str
    error_class: str | None
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None


class NotificationReceiptPublic(SQLModel):
    id: uuid.UUID
    notification_id: uuid.UUID
    provider: str
    provider_event_id: str
    provider_message_id: str
    event_type: str
    signature_verified: bool
    occurred_at: datetime
    received_at: datetime


class NotificationPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID | None
    visit_id: uuid.UUID | None
    publication_id: uuid.UUID | None
    portal_invitation_id: uuid.UUID | None = None
    purpose: str
    channel: NotificationChannel
    destination_masked: str
    state: NotificationState
    available_at: datetime
    submitted_at: datetime | None
    delivered_at: datetime | None
    failed_at: datetime | None
    acknowledged_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    attempt_count: int = 0
    attempts: list[NotificationAttemptPublic] = Field(default_factory=list)
    receipts: list[NotificationReceiptPublic] = Field(default_factory=list)


class PatientSharingRequestCreate(SQLModel):
    entry_version_id: uuid.UUID


class PatientSharingApprovalCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    medication_reviews: list[MedicationReviewAttestation] = Field(default_factory=list)


class PatientSharingRequestPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    entry_title: str
    entry_section: str
    entry_origin: str
    requested_by_name: str
    status: str
    created_at: datetime
    reviewed_at: datetime | None
    reviewed_by_name: str | None = None
    publication_id: uuid.UUID | None = None


class DecisionExplanationPublic(SQLModel):
    highlight_id: uuid.UUID
    review_state: Literal["ready", "review_required", "abstained"]
    output_type: str
    support_state: str
    risk: dict[str, object]
    confidence: dict[str, object]
    importance: dict[str, object]
    abstention_reason: str | None
    current_confidence_state: Literal["qualified", "unavailable", "review_required"] = (
        "unavailable"
    )
    confidence_qualification_reasons: list[str] = Field(default_factory=list)
    confidence_qualified_at: datetime | None = None
    safety_review_required: bool = False


class ReviewRequestCreate(SQLModel):
    reason: str = Field(min_length=3, max_length=500)


class ImportanceImpressionCreate(SQLModel):
    highlight_id: uuid.UUID
    view_event_id: str = Field(min_length=8, max_length=120)
    rank: int = Field(ge=1)
    surface: Literal["current_priorities", "clinical_review"] = "current_priorities"
    exposure_probability: float = Field(default=1.0, gt=0.0, le=1.0)
    visible_ratio: float = Field(ge=0.5, le=1.0)
    visible_duration_ms: int = Field(ge=2_000, le=600_000)


class ConflictResolve(SQLModel):
    resolution: str = Field(min_length=3, max_length=500)
    correction_entry_id: uuid.UUID


class ConflictPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    fact_type: str
    normalized_key: str
    severity: str
    status: str
    left_entry_id: uuid.UUID
    right_entry_id: uuid.UUID
    left_pointer_id: uuid.UUID | None
    right_pointer_id: uuid.UUID | None
    resolution: str | None
    created_at: datetime
    left_assertion_scope: str | None = None
    right_assertion_scope: str | None = None
    left_polarity: str | None = None
    right_polarity: str | None = None
    left_allergy_category: Literal["drug", "food", "environmental"] | None = None
    right_allergy_category: Literal["drug", "food", "environmental"] | None = None
    left_origin: str | None = None
    right_origin: str | None = None
    left_source_role: str | None = None
    right_source_role: str | None = None
    left_source_section: str | None = None
    right_source_section: str | None = None
    left_source_language: str | None = None
    right_source_language: str | None = None
    left_assertion_state: Literal["active", "superseded"] | None = None
    right_assertion_state: Literal["active", "superseded"] | None = None
    left_effective_time: datetime | None = None
    right_effective_time: datetime | None = None
    left_recorded_at: datetime | None = None
    right_recorded_at: datetime | None = None
    review_required: bool = True
    safety_review_state: Literal["ready", "review_required", "critical_unresolved"] = (
        "critical_unresolved"
    )
    current_confidence_state: Literal["qualified", "unavailable", "review_required"] = (
        "review_required"
    )
    current_confidence_reasons: list[str] = Field(default_factory=list)


class ClinicalFactAssertionPublic(SQLModel):
    id: uuid.UUID
    fact_type: str
    subject: str
    normalized_value: str
    polarity: str = "present"
    assertion_scope: str = "specific_substance"
    allergy_category: Literal["drug", "food", "environmental"] | None = None
    source_language: str = "und"
    source_role: str | None = None
    source_section: str | None = None
    assertion_state: Literal["active", "superseded"] = "active"
    superseded_by_assertion_id: uuid.UUID | None = None
    superseded_at: datetime | None = None
    clinical_status: str
    effective_time: datetime | None
    origin: str
    source_entry_version_id: uuid.UUID
    provenance_pointer_id: uuid.UUID
    medication: str | None = None
    dose_value: float | None = None
    dose_unit: str | None = None
    route: str | None = None
    frequency: str | None = None


class ProvisionalSafetyAlertPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    session_id: uuid.UUID
    source_event_id: str
    source_start_offset: int
    source_end_offset: int
    source_language: str
    concept_code: str
    assertion_scope: str
    polarity: str
    severity: str
    state: Literal["pending", "confirmed", "dismissed", "superseded"]
    completed_segment_at: datetime
    detected_at: datetime
    reviewed_at: datetime | None
    review_reason_code: str | None
    confirmed_assertion_id: uuid.UUID | None


class ProvisionalSafetyAlertReviewRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    reason_code: ReasonCodeInput | None = None


class HighlightSupportReviewPublic(SQLModel):
    id: uuid.UUID
    highlight_id: uuid.UUID
    patient_id: uuid.UUID
    source_entry_version_id: uuid.UUID
    observed_current_version_id: uuid.UUID
    support_state: Literal["current", "historical", "superseded"]
    review_status: Literal["pending", "reaffirmed", "superseded"]
    reviewed_at: datetime | None
    created_at: datetime


class ImportanceCandidateExposurePublic(SQLModel):
    id: uuid.UUID
    candidate_set_id: str
    view_event_id: str
    patient_id: uuid.UUID
    highlight_id: uuid.UUID
    rank: int
    surface: str
    feature_keys: list[str]
    shadow_score: float
    protected: bool
    displayed: bool
    exposure_probability: float
    observed_at: datetime


class ImportanceExposureReportCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    window_hours: int = Field(default=24, ge=1, le=168)


class ImportanceExposureSurfacePublic(SQLModel):
    candidate_count: int = Field(ge=0)
    telemetry_count: int = Field(ge=0)
    displayed_count: int = Field(ge=0)
    protected_candidate_count: int = Field(ge=0)
    protected_displayed_count: int = Field(ge=0)
    ordinary_candidate_count: int = Field(ge=0)
    ordinary_displayed_count: int = Field(ge=0)
    missing_telemetry_count: int = Field(ge=0)
    duplicate_telemetry_count: int = Field(ge=0)


class ImportanceExposureQualificationReportPublic(SQLModel):
    id: uuid.UUID
    report_version: str
    window_start: datetime
    window_end: datetime
    source_candidate_set_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    telemetry_count: int = Field(ge=0)
    displayed_count: int = Field(ge=0)
    protected_candidate_count: int = Field(ge=0)
    protected_displayed_count: int = Field(ge=0)
    ordinary_candidate_count: int = Field(ge=0)
    ordinary_displayed_count: int = Field(ge=0)
    protected_recall: float = Field(ge=0, le=1)
    ordinary_recall: float = Field(ge=0, le=1)
    ordinary_exposure_rate: float = Field(ge=0, le=1)
    missing_telemetry_count: int = Field(ge=0)
    duplicate_telemetry_count: int = Field(ge=0)
    surfaces: dict[
        Literal["current_priorities", "clinical_review"],
        ImportanceExposureSurfacePublic,
    ]
    qualified: bool
    qualification_reasons: list[str] = Field(default_factory=list)
    current: bool
    current_reasons: list[str] = Field(default_factory=list)
    effective_mode: Literal["disabled", "shadow", "active"]
    expires_at: datetime
    created_at: datetime


class PlatformLogin(SQLModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class PlatformMePublic(SQLModel):
    user_id: uuid.UUID
    platform_admin_id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: Literal["platform_admin"] = "platform_admin"


class PlatformClinicPublic(SQLModel):
    id: uuid.UUID
    code: str
    name: str
    member_count: int
    patient_count: int


class ClinicInitialStaff(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role: Literal["admin", "clinician", "staff"] = "admin"


class ClinicOnboardingCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    code: str = Field(min_length=3, max_length=12)
    slug: str = Field(min_length=3, max_length=80)
    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(min_length=3, max_length=80)
    initial_staff: list[ClinicInitialStaff] = Field(min_length=1)
    worker_enabled: bool = True
    supported_languages: list[Literal["en", "ms", "nan", "zh", "cmn"]] = Field(
        default_factory=lambda: ["en", "ms", "nan", "zh"]
    )
    messaging_channels: list[NotificationChannel] = Field(default_factory=list)
    remote_text_egress_enabled: bool = False
    remote_audio_egress_enabled: bool = False
    calibration_required: bool = True
    formulary_template: Literal["nightingale-clinic-formulary-v1"] = (
        "nightingale-clinic-formulary-v1"
    )


class ClinicOperationalSettingPublic(SQLModel):
    clinic_id: uuid.UUID
    timezone: str
    worker_enabled: bool
    supported_languages: list[str]
    messaging_channels: list[str]
    remote_text_egress_enabled: bool
    remote_audio_egress_enabled: bool
    calibration_required: bool
    external_proxy_retention_days: int
    external_container_retention_days: int
    external_apm_retention_days: int
    external_observability_retention_evidence: Literal[
        "unqualified",
        "deterministic_fixture",
        "deployment_policy",
        "provider_contract",
    ]
    external_observability_retention_evidence_id: str
    formulary_template: str
    onboarding_status: Literal["draft", "ready", "blocked"]
    updated_at: datetime


class ClinicChannelCapabilityEvidencePublic(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    channel: NotificationChannel
    provider: str
    configured: bool
    production_safe: bool
    reason_code: str | None = None


class ClinicPreflightEvidencePublic(SQLModel):
    """Typed, PHI-free evidence supporting one onboarding preflight check."""

    model_config = SQLModelConfig(extra="forbid")

    evidence_id: str
    observed_at: datetime
    source: Literal["request", "runtime", "deployment", "stored_policy"]
    requested_code: str | None = None
    requested_slug: str | None = None
    timezone: str | None = None
    initial_staff_count: int | None = Field(default=None, ge=0)
    initial_admin_count: int | None = Field(default=None, ge=0)
    worker_enabled: bool | None = None
    worker_kind: str | None = None
    worker_version: str | None = None
    worker_source_commit: str | None = None
    worker_heartbeat_at: datetime | None = None
    worker_heartbeat_age_seconds: int | None = Field(default=None, ge=0)
    worker_heartbeat_max_age_seconds: int | None = Field(default=None, ge=1)
    requested_languages: list[str] = Field(default_factory=list)
    available_languages: list[str] = Field(default_factory=list)
    missing_languages: list[str] = Field(default_factory=list)
    channels: list[ClinicChannelCapabilityEvidencePublic] = Field(default_factory=list)
    remote_text_requested: bool | None = None
    remote_text_deployment_ready: bool | None = None
    remote_audio_requested: bool | None = None
    remote_audio_deployment_ready: bool | None = None
    local_asr_default: bool | None = None
    # Observed deployment configuration, reported verbatim. The 1..30 window is
    # enforced where it belongs — on the stored clinic setting and by the
    # preflight check itself — so an out-of-range value must remain reportable
    # rather than crash the endpoint that exists to flag it.
    proxy_retention_days: int | None = Field(default=None, ge=0)
    container_retention_days: int | None = Field(default=None, ge=0)
    apm_retention_days: int | None = Field(default=None, ge=0)
    retention_evidence: str | None = None
    retention_evidence_id: str | None = None
    formulary_template: str | None = None
    calibration_required: bool | None = None


class ClinicPreflightCheckPublic(SQLModel):
    key: Literal[
        "code",
        "timezone",
        "initial_staff",
        "worker",
        "languages",
        "messaging",
        "egress_policy",
        "observability_retention",
        "formulary",
        "calibration",
    ]
    passed: bool
    reason_code: str | None = None
    evidence: ClinicPreflightEvidencePublic


class ClinicPreflightPublic(SQLModel):
    clinic_id: uuid.UUID
    ready: bool
    checks: list[ClinicPreflightCheckPublic]
    settings: ClinicOperationalSettingPublic


class ClinicFormularyConceptCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    concept_code: str = Field(min_length=3, max_length=100)
    canonical_name: str = Field(min_length=1, max_length=255)
    multilingual_aliases: dict[str, list[str]]
    dose_unit: str = Field(min_length=1, max_length=20)
    minimum_single_dose: float
    maximum_single_dose: float
    permitted_routes: list[str] = Field(min_length=1, max_length=12)
    contraindicated_allergy_concepts: list[str] = Field(
        default_factory=list, max_length=100
    )


class ClinicFormularyVersionCreate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    version_code: str = Field(min_length=3, max_length=80)
    concepts: list[ClinicFormularyConceptCreate] = Field(min_length=1, max_length=1_000)


class ClinicFormularyQualificationRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    expected_content_sha256: str = Field(min_length=64, max_length=64)


class ClinicFormularyConceptPublic(SQLModel):
    id: uuid.UUID
    concept_code: str
    canonical_name: str
    multilingual_aliases: dict[str, list[str]]
    dose_unit: str
    minimum_single_dose: float
    maximum_single_dose: float
    permitted_routes: list[str]
    contraindicated_allergy_concepts: list[str]


class ClinicFormularyVersionPublic(SQLModel):
    id: uuid.UUID
    version_code: str
    status: Literal["draft", "active", "retired"]
    content_sha256: str
    computed_content_sha256: str | None
    digest_matches: bool
    qualification_state: Literal[
        "unqualified", "qualified", "invalid", "active", "retired"
    ]
    qualification_source: Literal["clinic_admin", "platform_template"] | None
    content_locked_at: datetime | None
    qualified_at: datetime | None
    effective_at: datetime
    retired_at: datetime | None
    concept_count: int
    concepts: list[ClinicFormularyConceptPublic] = Field(default_factory=list)


class ClinicFormularyVersionsPublic(SQLModel):
    data: list[ClinicFormularyVersionPublic]
    count: int


class ClinicFormularyReadinessPublic(SQLModel):
    ready: bool
    reason_code: str | None = None
    active_version_id: uuid.UUID | None = None
    version_code: str | None = None
    content_sha256: str | None = None
    qualification_source: Literal["clinic_admin", "platform_template"] | None = None


class ProviderCircuitPublic(SQLModel):
    provider: str
    capability: str
    state: Literal["closed", "open", "half_open"]
    consecutive_failures: int
    last_error_class: str | None
    opened_at: datetime | None
    next_probe_at: datetime | None
    last_success_at: datetime | None
    updated_at: datetime


class PlatformClinicsPublic(SQLModel):
    data: list[PlatformClinicPublic]
    count: int


class PlatformAuditPublic(SQLModel):
    id: uuid.UUID
    action: str
    target_clinic_id: uuid.UUID | None
    target_patient_id: uuid.UUID | None
    request_id: str
    created_at: datetime
    reason_code: str = "not_specified"


class PlatformAuditsPublic(SQLModel):
    data: list[PlatformAuditPublic]
    count: int


class PatientsPublic(SQLModel):
    data: list[PatientPublic]
    count: int
    offset: int = 0
    limit: int = 50


class PatientsSearchRequest(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    search: str | None = Field(default=None, max_length=100)
    visit_scope: Literal["all", "today", "previous"] = "all"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class EntryCreate(SQLModel):
    patient_id: uuid.UUID
    section: Section
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=200_000)
    patient_facing: bool = False
    origin: EntryOrigin = "human"
    entry_type: EntryType | None = None
    occurred_at: datetime | None = None
    supersedes_entry_id: uuid.UUID | None = None
    conflicts_with_entry_id: uuid.UUID | None = None


class EntryPatch(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    patient_facing: bool | None = None


EntryAuthorRole = Literal["patient", "staff", "clinician", "system"]
EntryProvenanceStatus = Literal["resolved", "archived", "unavailable"]


class EntryProvenancePublic(SQLModel):
    """Direct immutable source metadata for a derived timeline entry.

    AI summaries point at the source message/version consumed by the AI run.
    ``exact_quote`` is therefore the exact source message, not a claim that the
    generated summary itself is an exact quotation.
    """

    source_entry_id: uuid.UUID | None
    source_entry_version_id: uuid.UUID | None
    exact_quote: str | None
    status: EntryProvenanceStatus


class EntryPublic(SQLModel):
    id: uuid.UUID
    clinic_id: uuid.UUID
    patient_id: uuid.UUID
    section: str
    origin: str
    entry_type: EntryType
    author_role: EntryAuthorRole
    provenance: EntryProvenancePublic | None = None
    patient_facing: bool
    version_id: uuid.UUID
    version_no: int
    title: str
    content: str
    author_id: uuid.UUID
    created_at: datetime
    occurred_at: datetime


class PatientTimelineEntry(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    section: str
    entry_type: EntryType
    author_role: EntryAuthorRole
    provenance: EntryProvenancePublic | None = None
    patient_facing: bool
    version_id: uuid.UUID
    version_no: int
    title: str
    content: str
    created_at: datetime
    occurred_at: datetime
    approval_receipt: dict[str, object] | None = None


class PatientTimeline(SQLModel):
    data: list[PatientTimelineEntry]
    count: int


class EntryVersionPublic(SQLModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    version_no: int
    title: str
    content: str
    content_sha256: str
    author_id: uuid.UUID
    reverted_from_version_id: uuid.UUID | None
    created_at: datetime


class EntryVersionsPublic(SQLModel):
    data: list[EntryVersionPublic]
    count: int


class DiffPublic(SQLModel):
    from_version_id: uuid.UUID
    to_version_id: uuid.UUID
    unified_diff: str


class AnchorInput(SQLModel):
    entry_version_id: uuid.UUID
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    exact_quote: str
    prefix: str = ""
    suffix: str = ""


class CommentCreate(AnchorInput):
    body: str = Field(min_length=1, max_length=20_000)
    parent_id: uuid.UUID | None = None
    mentioned_user_ids: list[uuid.UUID] = Field(default_factory=list)
    assigned_membership_id: uuid.UUID | None = None


class CommentPublic(SQLModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    parent_id: uuid.UUID | None
    author_id: uuid.UUID
    body: str
    anchor_state: str
    review_required: bool
    assigned_membership_id: uuid.UUID | None
    revision: int = Field(ge=1)
    mentioned_user_ids: list[uuid.UUID] = Field(default_factory=list)
    resolved_at: datetime | None
    created_at: datetime


class AssignmentUpdate(SQLModel):
    assigned_membership_id: uuid.UUID | None


class EditorPresenceHeartbeatCreate(SQLModel):
    """A content-free signal that an actor is editing one immutable version."""

    model_config = SQLModelConfig(extra="forbid")

    entry_version_id: uuid.UUID


class EditorPresencePublic(SQLModel):
    """Short-lived editor identity projection delivered over clinic SSE."""

    clinic_id: uuid.UUID
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    actor_id: uuid.UUID
    actor_role: Literal["staff", "clinician"]
    actor_display_name: str = Field(min_length=1, max_length=255)
    expires_at: datetime


class HighlightCreate(AnchorInput):
    label: str = Field(min_length=1, max_length=500)
    critical: bool = False
    patient_facing: bool = False
    feature_keys: list[str] = Field(default_factory=list, max_length=20)
    unresolved: bool = False
    clinician_confirmed: bool = False


class HighlightPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    entry_id: uuid.UUID
    source_entry_version_id: uuid.UUID
    label: str
    status: str
    pinned: bool
    critical: bool
    patient_facing: bool
    anchor_state: str
    review_required: bool
    feature_keys: list[str]
    base_score: float
    learned_score: float
    final_score: float
    risk_reason: RiskReason
    unresolved: bool
    clinician_confirmed: bool
    provenance_pointer_id: uuid.UUID
    support_state: Literal["current", "historical", "superseded"] = "current"
    support_review_required: bool = False
    current_priority_eligible: bool = True
    safety_review_required: bool = False
    current_confidence_state: Literal["qualified", "unavailable", "review_required"] = (
        "unavailable"
    )
    current_confidence_reasons: list[str] = Field(default_factory=list)


class ProvenanceResolved(SQLModel):
    id: uuid.UUID
    entry_version_id: uuid.UUID
    state: str
    review_required: bool
    start_offset: int
    end_offset: int
    exact_quote: str
    prefix: str
    suffix: str
    quote_sha256: str
    audio_asset_id: uuid.UUID | None
    audio_start_ms: int | None
    audio_end_ms: int | None
    support_state: Literal["current", "historical", "superseded"] = "current"


class PatientGlanceCard(SQLModel):
    highlight_id: uuid.UUID
    label: str
    provenance_pointer_id: uuid.UUID
    support_state: Literal["current", "historical", "superseded"] = "current"
    support_review_required: bool = False
    current_priority_eligible: bool = True


class ClinicalGlanceCard(SQLModel):
    highlight_id: uuid.UUID
    label: str
    critical: bool
    pinned: bool
    risk_reason: RiskReason
    provenance_pointer_id: uuid.UUID
    score_components: dict[str, float]
    review_state: Literal["ready", "review_required", "abstained"] = "ready"
    risk: dict[str, object] = Field(default_factory=dict)
    confidence: dict[str, object] = Field(default_factory=dict)
    importance: dict[str, object] = Field(default_factory=dict)
    abstention_reason: str | None = None
    support_state: Literal["current", "historical", "superseded"] = "current"
    fallback_kind: Literal["stored", "rule_derived"] | None = None
    support_review_required: bool = False
    current_priority_eligible: bool = True
    current_confidence_state: Literal["qualified", "unavailable", "review_required"] = (
        "unavailable"
    )
    current_confidence_reasons: list[str] = Field(default_factory=list)


class GlancePublic(SQLModel):
    patient_id: uuid.UUID
    source: Literal["precomputed"] = "precomputed"
    generated_at: datetime
    cards: list[PatientGlanceCard]
    importance_mode: Literal["disabled", "shadow", "active"]
    freshness_state: Literal["fresh", "stale", "unavailable"] = "fresh"
    age_seconds: int = 0
    provider_outage: bool = False
    outage_message: str | None = None
    fallback_kind: Literal["stored", "rule_derived"] | None = None
    safety_review_required: bool = False
    current_confidence_state: Literal["qualified", "unavailable", "review_required"] = (
        "unavailable"
    )
    current_confidence_reasons: list[str] = Field(default_factory=list)


class ClinicalGlancePublic(SQLModel):
    patient_id: uuid.UUID
    source: Literal["precomputed"] = "precomputed"
    generated_at: datetime
    cards: list[ClinicalGlanceCard]
    review_cards: list[ClinicalGlanceCard] = Field(default_factory=list)
    importance_mode: Literal["disabled", "shadow", "active"]
    freshness_state: Literal["fresh", "stale", "unavailable"] = "fresh"
    age_seconds: int = 0
    provider_outage: bool = False
    outage_message: str | None = None
    fallback_kind: Literal["stored", "rule_derived"] | None = None
    safety_review_required: bool = False
    current_confidence_state: Literal["qualified", "unavailable", "review_required"] = (
        "unavailable"
    )
    current_confidence_reasons: list[str] = Field(default_factory=list)


class AIIngestRequest(SQLModel):
    source_entry_version_id: uuid.UUID
    interaction_type: InteractionType = "care_note"


class AIRunPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    source_entry_version_id: uuid.UUID
    provider: str
    model: str
    review_model: str | None
    review_status: str
    status: str
    risk_tier: str
    fallback_reason: str | None
    needs_review: bool
    output_entry_id: uuid.UUID | None
    output_entry_version_id: uuid.UUID | None
    warnings: list[str]
    created_at: datetime


class JobRetryAttemptPublic(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    attempt: int = Field(ge=0)
    error_code: str = Field(max_length=80)
    error_class: str = Field(max_length=80)
    attempted_at: datetime
    next_retry_at: datetime | None = None
    source_job_id: uuid.UUID | None = None
    recovery_job_id: uuid.UUID | None = None
    provider: str | None = Field(default=None, max_length=60)
    capability: str | None = Field(default=None, max_length=60)
    circuit_state: Literal["closed", "open", "half_open"] | None = None


class JobPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    kind: str
    state: str
    attempt_count: int
    max_attempts: int
    error_code: str | None
    ai_run: AIRunPublic | None = None
    created_at: datetime
    updated_at: datetime
    error_class: str | None = None
    next_run_at: datetime | None = None
    provider_outage: bool = False
    provider_circuit: ProviderCircuitPublic | None = None
    retry_history: list[JobRetryAttemptPublic] = Field(default_factory=list)
    retry_history_invalid_count: int = Field(default=0, ge=0)
    delayed_at: datetime | None = None
    timed_out_at: datetime | None = None
    last_attempt_at: datetime | None = None
    outage_started_at: datetime | None = None
    outage_age_seconds: int = 0
    retry_after_seconds: int | None = None
    visible_state: (
        Literal["queued", "running", "delayed", "timed_out", "failed"] | None
    ) = None
    current_confidence_state: Literal["qualified", "unavailable", "review_required"] = (
        "unavailable"
    )
    current_confidence_reasons: list[str] = Field(default_factory=list)
    safety_review_required: bool = False


class ImportanceFeedbackCreate(SQLModel):
    signal: Literal["dismiss"]
    reason: Literal[
        "not_relevant", "outdated", "already_addressed", "too_busy_to_review"
    ]


class DecayCandidatePublic(SQLModel):
    entry_version_id: uuid.UUID
    entry_id: uuid.UUID
    storage_tier: Literal["hot", "warm", "cold"]
    age_days: int
    eligible_for_cold: bool
    protected_reasons: list[str]


class DecayPreviewPublic(SQLModel):
    policy_version: str = "nightingale-decay-v1"
    candidates: list[DecayCandidatePublic]
    count: int


class DecayArchiveRequest(SQLModel):
    entry_version_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    dry_run: bool = False


class DecayArchivePublic(SQLModel):
    decay_run_id: uuid.UUID
    candidate_count: int
    archived_count: int
    error_count: int


class RehydratePublic(SQLModel):
    entry_version_id: uuid.UUID
    storage_tier: str
    content_sha256: str


VoiceCaptureKind = Literal["patient", "clinical"]
VoiceCaptureRole = Literal["patient", "staff", "clinician"]


class VoiceSessionCreate(SQLModel):
    patient_id: uuid.UUID
    capture_kind: VoiceCaptureKind
    synthetic_fixture: bool = False
    fixture_id: str | None = Field(default=None, max_length=100)
    remote_audio_consent: bool = False


AudioQualityUnavailableReason = Literal[
    "AUDIO_ASSET_NOT_AVAILABLE",
    "AUDIO_QUALITY_METADATA_INVALID",
]


class AudioQualityPublic(SQLModel):
    """Allowlisted, typed source-audio quality evidence.

    Measurements describe the decoded source before normalization.  The
    denoised working copy never replaces the encrypted source evidence, and a
    review signal remains visible even when denoising was applied.
    """

    model_config = SQLModelConfig(extra="forbid")

    measurement_stage: Literal["decoded-pre-normalization"]
    processing_chain_version: str = Field(min_length=1, max_length=100)
    rms: float = Field(ge=0)
    noise_floor_dbfs: float = Field(le=0)
    estimated_snr_db: float
    clipping_ratio: float = Field(ge=0, le=1)
    silence_ratio: float = Field(ge=0, le=1)
    silence_review: bool
    clipping_review: bool
    low_signal_review: bool
    noise_review: bool
    multi_device_overlap_review: bool
    denoise_applied: bool
    review_required: bool


class VoiceSessionPublic(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    capture_kind: str
    state: str
    patient_summary: str | None = None
    warning_codes: list[str] = Field(default_factory=list)
    error_code: str | None = None
    live_transcript_status: LiveTranscriptStatus = "not_started"
    live_transcript_reason_code: str | None = None
    current_transcript_revision_id: uuid.UUID | None = None
    published_entry_id: uuid.UUID | None = None
    audio_quality: AudioQualityPublic | None
    audio_quality_unavailable_reason: AudioQualityUnavailableReason | None
    remote_audio_consent_recorded: bool = False
    remote_audio_consent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class VoiceDeviceJoin(SQLModel):
    client_device_id: str = Field(min_length=1, max_length=120)
    capture_role: VoiceCaptureRole
    expected_patient_id: uuid.UUID
    expected_capture_kind: VoiceCaptureKind


class VoiceDevicePublic(SQLModel):
    id: uuid.UUID
    session_id: uuid.UUID
    client_device_id: str
    capture_role: str
    created_at: datetime


class VoiceDeviceAbandonPublic(SQLModel):
    device_id: uuid.UUID
    abandoned: Literal[True] = True


class VoiceDeviceSeal(SQLModel):
    last_chunk_index: int = Field(ge=0, le=21_600)


class VoiceDeviceSealPublic(SQLModel):
    device_id: uuid.UUID
    last_chunk_index: int
    sealed: Literal[True] = True


class AudioChunkAck(SQLModel):
    chunk_index: int
    acknowledged: bool = True
    duplicate: bool = False


class VoiceDeviceChunkStatus(SQLModel):
    device_id: uuid.UUID
    client_device_id: str
    received_indices: list[int]
    last_declared_chunk_index: int | None


class VoiceChunkStatus(SQLModel):
    uploaded_chunks: int
    devices: list[VoiceDeviceChunkStatus]


class VoiceFinalizeDevice(SQLModel):
    device_id: uuid.UUID
    # Two-second chunks: cap declarations at twelve hours to bound missing-list
    # work even for an authenticated but malformed request.
    last_chunk_index: int = Field(ge=0, le=21_600)


class VoiceFinalizeRequest(SQLModel):
    devices: list[VoiceFinalizeDevice] = Field(min_length=1, max_length=8)


class VoiceFinalizePublic(SQLModel):
    session_id: uuid.UUID
    state: str
    job_id: uuid.UUID


class TranscriptLanguageSpanPublic(SQLModel):
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    language_code: Literal["en", "ms", "nan", "zh", "cmn", "und"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    detection_source: Literal[
        "provider_hint",
        "lexicon_rule",
        "lexicon_and_provider",
        "mixed_rule",
        "unavailable",
        "human_review",
    ]
    review_required: bool = False


class TranscriptSegmentPublic(SQLModel):
    id: uuid.UUID
    ordinal: int
    text: str
    text_start: int
    text_end: int
    start_ms: int
    end_ms: int
    speaker_id: str | None
    speaker_ids: list[str] = Field(default_factory=list)
    detected_language: str | None
    source_language: str = "und"
    language_confidence: float | None = None
    language_spans: list[TranscriptLanguageSpanPublic] = Field(default_factory=list)
    confidence: float | None
    confidence_source: str
    overlap_group_id: str | None
    provider: str
    model: str


class ClinicalFactPublic(SQLModel):
    id: uuid.UUID
    ordinal: int
    fact_type: str
    value: str
    exact_quote: str
    transcript_start: int
    transcript_end: int
    audio_asset_id: uuid.UUID
    audio_start_ms: int
    audio_end_ms: int
    status: str
    stale: bool
    medication: str | None = None
    dose_value: float | None = None
    dose_unit: str | None = None
    route: str | None = None
    frequency: str | None = None


class TranscriptRevisionPublic(SQLModel):
    id: uuid.UUID
    session_id: uuid.UUID
    revision_no: int
    previous_revision_id: uuid.UUID | None
    text: str
    text_sha256: str
    summary: str | None
    provider: str
    model: str
    detected_language: str | None
    status: str
    needs_review: bool
    stale: bool
    fallback: bool
    warning_codes: list[str]
    audio_quality: AudioQualityPublic | None
    audio_quality_unavailable_reason: AudioQualityUnavailableReason | None
    segments: list[TranscriptSegmentPublic]
    facts: list[ClinicalFactPublic]
    created_at: datetime


class TranscriptCorrection(SQLModel):
    expected_revision_id: uuid.UUID
    text: str = Field(min_length=1, max_length=500_000)


class VoiceReanalyzeRequest(SQLModel):
    expected_revision_id: uuid.UUID


class VoiceReanalyzePublic(SQLModel):
    session_id: uuid.UUID
    job_id: uuid.UUID
    state: str


class VoicePublishRequest(SQLModel):
    expected_revision_id: uuid.UUID
    medication_reviews: list[MedicationReviewAttestation] = Field(default_factory=list)


class VoiceRemoteAudioConsentUpdate(SQLModel):
    model_config = SQLModelConfig(extra="forbid")

    consent: bool


class VoicePublishPublic(SQLModel):
    session_id: uuid.UUID
    entry_id: uuid.UUID
    entry_version_id: uuid.UUID
    state: Literal["published"] = "published"


class LiveTranscriptAvailability(SQLModel):
    available: bool
    status: Literal["available", "unavailable", "needs_review", "replaced"]
    reason_code: str | None = None
    provider: str | None = None
    model: str | None = None
    provisional: Literal[True] = True
