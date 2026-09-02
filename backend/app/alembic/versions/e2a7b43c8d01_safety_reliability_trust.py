"""Add safety, provider reliability, and trust requalification state.

Revision ID: e2a7b43c8d01
Revises: e1f6a32b7c90
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2a7b43c8d01"
down_revision: str | None = "e1f6a32b7c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_id() -> sa.Column:
    return sa.Column(
        "clinic_id",
        postgresql.UUID(),
        sa.ForeignKey("clinics.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    for table in ("audit_events", "platform_audit_events"):
        op.add_column(
            table,
            sa.Column(
                "reason_code",
                sa.String(80),
                nullable=False,
                server_default="not_specified",
            ),
        )
        op.add_column(table, sa.Column("clinical_rationale_ciphertext", sa.LargeBinary()))

    op.add_column(
        "patient_glance_snapshots",
        sa.Column("freshness_state", sa.String(20), nullable=False, server_default="fresh"),
    )
    op.add_column(
        "patient_glance_snapshots",
        sa.Column("provider_outage", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "patient_glance_snapshots",
        sa.Column("outage_started_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "patient_glance_snapshots", sa.Column("fallback_kind", sa.String(30))
    )

    op.add_column("jobs", sa.Column("error_class", sa.String(80)))
    op.add_column(
        "jobs",
        sa.Column("provider_outage", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "retry_history_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    for column in ("delayed_at", "timed_out_at", "last_attempt_at"):
        op.add_column("jobs", sa.Column(column, sa.DateTime(timezone=True)))
    op.add_column("job_attempts", sa.Column("error_class", sa.String(80)))
    op.add_column(
        "job_attempts", sa.Column("retry_scheduled_at", sa.DateTime(timezone=True))
    )
    op.add_column("job_attempts", sa.Column("duration_ms", sa.Integer()))

    op.add_column(
        "highlights",
        sa.Column("support_state", sa.String(20), nullable=False, server_default="current"),
    )
    op.add_column(
        "highlights",
        sa.Column(
            "support_review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "highlights",
        sa.Column(
            "current_priority_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_check_constraint(
        "ck_highlight_support_state_value",
        "highlights",
        "support_state IN ('current','historical','superseded')",
    )
    op.create_check_constraint(
        "ck_highlight_review_removes_priority",
        "highlights",
        "NOT support_review_required OR NOT current_priority_eligible",
    )

    op.add_column(
        "clinical_fact_assertions",
        sa.Column(
            "assertion_scope",
            sa.String(40),
            nullable=False,
            server_default="specific_substance",
        ),
    )
    op.add_column(
        "clinical_fact_assertions",
        sa.Column("source_language", sa.String(20), nullable=False, server_default="und"),
    )
    op.add_column(
        "clinical_fact_assertions",
        sa.Column("assertion_state", sa.String(20), nullable=False, server_default="active"),
    )
    op.add_column(
        "clinical_fact_assertions",
        sa.Column("superseded_by_assertion_id", postgresql.UUID()),
    )
    op.add_column(
        "clinical_fact_assertions",
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_fact_assertion_scope",
        "clinical_fact_assertions",
        "assertion_scope IN ('specific_substance','drug_allergies','all_allergies')",
    )
    op.create_check_constraint(
        "ck_fact_assertion_state",
        "clinical_fact_assertions",
        "assertion_state IN ('active','superseded')",
    )
    op.create_check_constraint(
        "ck_fact_assertion_not_self_superseding",
        "clinical_fact_assertions",
        "superseded_by_assertion_id IS NULL OR superseded_by_assertion_id <> id",
    )
    op.create_foreign_key(
        "fk_fact_assertion_superseded_by",
        "clinical_fact_assertions",
        "clinical_fact_assertions",
        ["clinic_id", "superseded_by_assertion_id"],
        ["clinic_id", "id"],
    )

    op.add_column(
        "patient_publications",
        sa.Column(
            "medication_review_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "patient_publications",
        sa.Column(
            "medication_review_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "patient_publications",
        sa.Column("medication_reviewed_by_membership_id", postgresql.UUID()),
    )
    op.add_column(
        "patient_publications",
        sa.Column("medication_reviewed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "patient_publications", sa.Column("correction_reason_code", sa.String(80))
    )
    op.add_column(
        "patient_publications",
        sa.Column("withdrawn_by_membership_id", postgresql.UUID()),
    )
    op.create_foreign_key(
        "fk_patient_publication_medication_reviewer",
        "patient_publications",
        "clinic_memberships",
        ["clinic_id", "medication_reviewed_by_membership_id"],
        ["clinic_id", "id"],
    )
    op.create_foreign_key(
        "fk_patient_publication_withdrawer",
        "patient_publications",
        "clinic_memberships",
        ["clinic_id", "withdrawn_by_membership_id"],
        ["clinic_id", "id"],
    )

    op.add_column(
        "transcript_segments",
        sa.Column("source_language", sa.String(20), nullable=False, server_default="und"),
    )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "ALTER TABLE transcript_segments "
            "DISABLE TRIGGER trg_transcript_segments_append_only"
        )
    )
    try:
        # The existing voice schema seals transcript segments against UPDATE.
        # Run this one-time metadata backfill inside a savepoint so an error
        # leaves the outer migration transaction usable by the finally block.
        with bind.begin_nested():
            bind.execute(
                sa.text(
                    """
        UPDATE transcript_segments
        SET source_language = CASE
          WHEN detected_language IS NULL OR btrim(detected_language) = '' THEN 'und'
          ELSE lower(split_part(detected_language, '-', 1))
        END
                    """
                )
            )
    finally:
        bind.execute(
            sa.text(
                "ALTER TABLE transcript_segments "
                "ENABLE TRIGGER trg_transcript_segments_append_only"
            )
        )
    op.add_column(
        "transcript_segments", sa.Column("language_confidence", sa.Float())
    )

    op.add_column(
        "voice_sessions", sa.Column("remote_audio_consent_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "voice_sessions",
        sa.Column("remote_audio_consent_by_id", postgresql.UUID()),
    )
    op.create_foreign_key(
        "voice_sessions_remote_audio_consent_by_id_fkey",
        "voice_sessions",
        "users",
        ["remote_audio_consent_by_id"],
        ["id"],
    )

    op.create_table(
        "provider_circuit_states",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("capability", sa.String(80), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="closed"),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_class", sa.String(80)),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("next_probe_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('closed','open','half_open')", name="ck_provider_circuit_state"
        ),
        sa.UniqueConstraint(
            "clinic_id", "provider", "capability", name="uq_provider_circuit"
        ),
    )
    op.create_index(
        "ix_provider_circuit_probe",
        "provider_circuit_states",
        ["clinic_id", "state", "next_probe_at"],
    )

    op.create_table(
        "importance_candidate_exposures",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("highlight_id", postgresql.UUID(), nullable=False),
        sa.Column("viewer_membership_id", postgresql.UUID(), nullable=False),
        sa.Column("view_event_id", sa.String(120), nullable=False),
        sa.Column("candidate_set_id", sa.String(120), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column(
            "surface", sa.String(60), nullable=False, server_default="current_priorities"
        ),
        sa.Column(
            "feature_keys_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("shadow_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("displayed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "exposure_probability", sa.Float(), nullable=False, server_default="0"
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "candidate_set_id",
            "highlight_id",
            name="uq_importance_candidate_exposure",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_importance_candidate_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_importance_candidate_highlight",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "viewer_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_candidate_viewer",
        ),
    )
    op.create_index(
        "ix_importance_candidate_observed",
        "importance_candidate_exposures",
        ["clinic_id", "patient_id", "observed_at"],
    )

    op.create_table(
        "highlight_support_reviews",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("highlight_id", postgresql.UUID(), nullable=False),
        sa.Column("source_entry_version_id", postgresql.UUID(), nullable=False),
        sa.Column("observed_current_version_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "support_state", sa.String(20), nullable=False, server_default="historical"
        ),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reviewed_by_membership_id", postgresql.UUID()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "support_state IN ('current','historical','superseded')",
            name="ck_highlight_support_state",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending','reaffirmed','superseded')",
            name="ck_highlight_support_review_status",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "highlight_id",
            "observed_current_version_id",
            name="uq_highlight_support_observation",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_highlight_support_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_highlight_support_highlight",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "source_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_highlight_support_source",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "observed_current_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_highlight_support_current",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "reviewed_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_highlight_support_reviewer",
        ),
    )
    op.create_index(
        "ix_highlight_support_pending",
        "highlight_support_reviews",
        ["clinic_id", "patient_id", "review_status"],
    )

    op.create_table(
        "provisional_safety_alerts",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("session_id", postgresql.UUID(), nullable=False),
        sa.Column("source_event_id", sa.String(160), nullable=False),
        sa.Column("source_text_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("source_text_sha256", sa.String(64), nullable=False),
        sa.Column("source_start_offset", sa.Integer(), nullable=False),
        sa.Column("source_end_offset", sa.Integer(), nullable=False),
        sa.Column("source_language", sa.String(20), nullable=False, server_default="und"),
        sa.Column("concept_code", sa.String(120), nullable=False),
        sa.Column(
            "assertion_scope",
            sa.String(40),
            nullable=False,
            server_default="specific_substance",
        ),
        sa.Column("polarity", sa.String(20), nullable=False, server_default="present"),
        sa.Column("deduplication_key", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, server_default="critical"),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("completed_segment_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reviewed_by_membership_id", postgresql.UUID()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_reason_code", sa.String(80)),
        sa.Column("confirmed_assertion_id", postgresql.UUID()),
        sa.CheckConstraint(
            "state IN ('pending','confirmed','dismissed','superseded')",
            name="ck_provisional_safety_alert_state",
        ),
        sa.CheckConstraint(
            "source_start_offset >= 0 AND source_end_offset >= source_start_offset",
            name="ck_provisional_safety_alert_span",
        ),
        sa.UniqueConstraint(
            "clinic_id", "session_id", "deduplication_key", name="uq_provisional_alert_dedup"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_provisional_alert_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "session_id"],
            ["voice_sessions.clinic_id", "voice_sessions.id"],
            name="fk_provisional_alert_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "reviewed_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_provisional_alert_reviewer",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "confirmed_assertion_id"],
            ["clinical_fact_assertions.clinic_id", "clinical_fact_assertions.id"],
            name="fk_provisional_alert_assertion",
        ),
    )
    op.create_index(
        "ix_provisional_alert_pending",
        "provisional_safety_alerts",
        ["clinic_id", "patient_id", "state", "detected_at"],
    )


def downgrade() -> None:
    op.drop_table("provisional_safety_alerts")
    op.drop_table("highlight_support_reviews")
    op.drop_table("importance_candidate_exposures")
    op.drop_table("provider_circuit_states")

    op.drop_constraint(
        "voice_sessions_remote_audio_consent_by_id_fkey",
        "voice_sessions",
        type_="foreignkey",
    )
    op.drop_column("voice_sessions", "remote_audio_consent_by_id")
    op.drop_column("voice_sessions", "remote_audio_consent_at")

    op.drop_column("transcript_segments", "language_confidence")
    op.drop_column("transcript_segments", "source_language")

    op.drop_constraint(
        "fk_patient_publication_withdrawer", "patient_publications", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_patient_publication_medication_reviewer",
        "patient_publications",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_highlight_review_removes_priority", "highlights", type_="check"
    )
    op.drop_constraint(
        "ck_highlight_support_state_value", "highlights", type_="check"
    )
    for column in (
        "withdrawn_by_membership_id",
        "correction_reason_code",
        "medication_reviewed_at",
        "medication_reviewed_by_membership_id",
        "medication_review_json",
        "medication_review_complete",
    ):
        op.drop_column("patient_publications", column)

    op.drop_constraint(
        "fk_fact_assertion_superseded_by",
        "clinical_fact_assertions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_fact_assertion_not_self_superseding",
        "clinical_fact_assertions",
        type_="check",
    )
    op.drop_constraint(
        "ck_fact_assertion_state", "clinical_fact_assertions", type_="check"
    )
    op.drop_constraint(
        "ck_fact_assertion_scope", "clinical_fact_assertions", type_="check"
    )
    for column in (
        "superseded_at",
        "superseded_by_assertion_id",
        "assertion_state",
        "source_language",
        "assertion_scope",
    ):
        op.drop_column("clinical_fact_assertions", column)

    for column in (
        "current_priority_eligible",
        "support_review_required",
        "support_state",
    ):
        op.drop_column("highlights", column)

    for column in ("duration_ms", "retry_scheduled_at", "error_class"):
        op.drop_column("job_attempts", column)
    for column in (
        "last_attempt_at",
        "timed_out_at",
        "delayed_at",
        "retry_history_json",
        "provider_outage",
        "error_class",
    ):
        op.drop_column("jobs", column)

    for column in (
        "fallback_kind",
        "outage_started_at",
        "provider_outage",
        "freshness_state",
    ):
        op.drop_column("patient_glance_snapshots", column)

    for table in ("platform_audit_events", "audit_events"):
        op.drop_column(table, "clinical_rationale_ciphertext")
        op.drop_column(table, "reason_code")
