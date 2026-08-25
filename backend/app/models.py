"""Nightingale database models and public API schemas.

Every domain row carries a clinic identifier. Application query scoping and the
PostgreSQL RLS migration are independent tenant boundaries.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import EmailStr
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


Role = Literal["patient", "staff", "clinician", "admin", "worker"]
Section = Literal["patient", "staff", "clinician", "system"]
EntryOrigin = Literal["human", "ai", "system"]
InteractionType = Literal[
    "care_note",
    "doctor_consult",
    "patient_insight",
    "voice_session",
]


class TenantRow(SQLModel):
    clinic_id: uuid.UUID = Field(foreign_key="clinics.id", ondelete="CASCADE")


class Clinic(SQLModel, table=True):
    __tablename__ = "clinics"
    __table_args__ = (
        UniqueConstraint("slug", name="clinics_slug_key"),
        Index("ix_clinics_slug", "slug"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(max_length=80)
    name: str = Field(max_length=255)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="users_email_key"),
        Index("ix_users_email", "email"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: EmailStr = Field(max_length=255)
    full_name: str | None = Field(default=None, max_length=255)
    hashed_password: str
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


class Patient(TenantRow, table=True):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_patient_clinic_id"),
        Index("ix_patient_clinic_created", "clinic_id", "created_at"),
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    display_name_ciphertext: bytes = Field(
        sa_column=Column(LargeBinary, nullable=False)
    )
    external_ref_hash: str = Field(max_length=64, index=True)
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


class Entry(TenantRow, table=True):
    __tablename__ = "entries"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_entry_clinic_id"),
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
    patient_facing: bool = Field(default=False)
    source_job_id: uuid.UUID | None = Field(default=None, index=True)
    current_version_id: uuid.UUID | None = None
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class EntryVersion(TenantRow, table=True):
    __tablename__ = "entry_versions"
    __table_args__ = (
        UniqueConstraint("clinic_id", "id", name="uq_entry_version_clinic_id"),
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
    created_by_id: uuid.UUID = Field(foreign_key="users.id")
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
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    highlight_id: uuid.UUID | None = Field(default=None)
    comment_id: uuid.UUID | None = Field(default=None)
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
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID
    left_entry_id: uuid.UUID
    right_entry_id: uuid.UUID
    status: str = Field(default="unresolved", max_length=20)
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


class DemoLoginRequest(SQLModel):
    persona: Literal["patient", "staff", "clinician", "admin", "worker", "other_staff"]


class MePublic(SQLModel):
    user_id: uuid.UUID
    email: EmailStr
    full_name: str | None
    clinic_id: uuid.UUID
    membership_id: uuid.UUID
    role: Role


class PatientPublic(SQLModel):
    id: uuid.UUID
    display_name: str


class PatientsPublic(SQLModel):
    data: list[PatientPublic]
    count: int


class EntryCreate(SQLModel):
    patient_id: uuid.UUID
    section: Section
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=200_000)
    patient_facing: bool = False
    origin: EntryOrigin = "human"
    supersedes_entry_id: uuid.UUID | None = None
    conflicts_with_entry_id: uuid.UUID | None = None


class EntryPatch(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    patient_facing: bool | None = None


class EntryPublic(SQLModel):
    id: uuid.UUID
    clinic_id: uuid.UUID
    patient_id: uuid.UUID
    section: str
    origin: str
    patient_facing: bool
    version_id: uuid.UUID
    version_no: int
    title: str
    content: str
    author_id: uuid.UUID
    created_at: datetime


class PatientTimelineEntry(SQLModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    section: str
    patient_facing: bool
    version_id: uuid.UUID
    version_no: int
    title: str
    content: str
    created_at: datetime


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
    mentioned_user_ids: list[uuid.UUID] = Field(default_factory=list)
    resolved_at: datetime | None
    created_at: datetime


class AssignmentUpdate(SQLModel):
    assigned_membership_id: uuid.UUID | None


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
    risk_reason: str
    unresolved: bool
    clinician_confirmed: bool
    provenance_pointer_id: uuid.UUID


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


class GlanceCard(SQLModel):
    highlight_id: uuid.UUID
    label: str
    critical: bool
    pinned: bool
    risk_reason: str
    provenance_pointer_id: uuid.UUID


class ClinicalGlanceCard(GlanceCard):
    score_components: dict[str, float]


class GlancePublic(SQLModel):
    patient_id: uuid.UUID
    source: Literal["precomputed"] = "precomputed"
    generated_at: datetime
    cards: list[GlanceCard]


class ClinicalGlancePublic(SQLModel):
    patient_id: uuid.UUID
    source: Literal["precomputed"] = "precomputed"
    generated_at: datetime
    cards: list[ClinicalGlanceCard]


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


class ImportanceFeedbackCreate(SQLModel):
    signal: Literal["dismiss"]


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
