"""Nightingale database models and public API schemas.

Every domain row carries a clinic identifier. Application query scoping and the
PostgreSQL RLS migration are independent tenant boundaries.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import EmailStr
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


Role = Literal["patient", "staff", "clinician", "admin", "worker"]
Section = Literal["patient", "staff", "clinician", "system"]
EntryOrigin = Literal["human", "ai", "system"]


class TenantRow(SQLModel):
    clinic_id: uuid.UUID = Field(foreign_key="clinics.id", index=True)


class Clinic(SQLModel, table=True):
    __tablename__ = "clinics"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(unique=True, index=True, max_length=80)
    name: str = Field(max_length=255)
    created_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: EmailStr = Field(unique=True, index=True, max_length=255)
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
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    role: str = Field(max_length=20, index=True)
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
    patient_id: uuid.UUID = Field(index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
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
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID = Field(index=True)
    section: str = Field(max_length=20, index=True)
    origin: str = Field(default="human", max_length=20, index=True)
    patient_facing: bool = Field(default=False, index=True)
    source_job_id: uuid.UUID | None = Field(default=None, index=True)
    current_version_id: uuid.UUID | None = Field(default=None, index=True)
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
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entry_id: uuid.UUID = Field(index=True)
    version_no: int = Field(ge=1)
    title_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    content_ciphertext: bytes | None = Field(
        default=None, sa_column=Column(LargeBinary, nullable=True)
    )
    content_sha256: str = Field(max_length=64)
    patient_facing: bool = Field(default=False, index=True)
    author_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    reverted_from_version_id: uuid.UUID | None = Field(default=None, index=True)
    storage_tier: str = Field(default="hot", max_length=10, index=True)
    archive_blob_id: uuid.UUID | None = Field(default=None, index=True)
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
    source_entry_id: uuid.UUID = Field(index=True)
    target_entry_id: uuid.UUID = Field(index=True)
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
    entry_id: uuid.UUID = Field(index=True)
    entry_version_id: uuid.UUID = Field(index=True)
    parent_id: uuid.UUID | None = Field(default=None)
    author_id: uuid.UUID = Field(foreign_key="users.id", index=True)
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
    comment_id: uuid.UUID = Field(index=True)
    mentioned_user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
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
    patient_id: uuid.UUID = Field(index=True)
    comment_id: uuid.UUID | None = Field(default=None)
    assignee_membership_id: uuid.UUID
    status: str = Field(default="open", max_length=20, index=True)
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
    patient_id: uuid.UUID = Field(index=True)
    entry_id: uuid.UUID = Field(index=True)
    source_entry_version_id: uuid.UUID = Field(index=True)
    label_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    status: str = Field(default="pending", max_length=20, index=True)
    pinned: bool = Field(default=False, index=True)
    critical: bool = Field(default=False, index=True)
    patient_facing: bool = Field(default=False, index=True)
    anchor_state: str = Field(default="resolved", max_length=20)
    review_required: bool = False
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
    entry_version_id: uuid.UUID = Field(index=True)
    start_offset: int
    end_offset: int
    exact_quote_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    prefix_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    suffix_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    quote_sha256: str = Field(max_length=64)
    anchor_state: str = Field(default="resolved", max_length=20)
    review_required: bool = False
    audio_asset_id: uuid.UUID | None = Field(default=None, index=True)
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
    patient_id: uuid.UUID = Field(index=True)
    left_entry_id: uuid.UUID
    right_entry_id: uuid.UUID
    status: str = Field(default="unresolved", max_length=20, index=True)
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
    actor_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    action: str = Field(max_length=80, index=True)
    resource_type: str = Field(max_length=40)
    resource_id: uuid.UUID = Field(index=True)
    metadata_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
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
    patient_id: uuid.UUID = Field(index=True)
    payload_ciphertext: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    source_event_sequence: int | None = None
    generated_at: datetime = Field(
        default_factory=get_datetime_utc,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class DomainEvent(TenantRow, table=True):
    __tablename__ = "domain_events"
    __table_args__ = (
        Index("ix_domain_event_clinic_sequence", "clinic_id", "sequence_no"),
    )
    sequence_no: int | None = Field(
        default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True)
    )
    id: uuid.UUID = Field(default_factory=uuid.uuid4, unique=True, index=True)
    event_type: str = Field(max_length=100, index=True)
    aggregate_type: str = Field(max_length=40)
    aggregate_id: uuid.UUID = Field(index=True)
    actor_id: uuid.UUID = Field(foreign_key="users.id")
    payload_json: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
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


class GlancePublic(SQLModel):
    patient_id: uuid.UUID
    source: Literal["precomputed"] = "precomputed"
    generated_at: datetime
    cards: list[GlanceCard]
