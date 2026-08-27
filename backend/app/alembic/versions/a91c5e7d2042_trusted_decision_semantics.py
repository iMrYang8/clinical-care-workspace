"""Add evidence-gated decision, calibration, and sharing records.

Revision ID: a91c5e7d2042
Revises: f3a8c71d2e40
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a91c5e7d2042"
down_revision: str | None = "f3a8c71d2e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TENANT_TABLES = (
    "clinical_fact_assertions",
    "evaluation_runs",
    "calibration_reports",
    "calibration_buckets",
    "redaction_evaluation_runs",
    "patient_sharing_requests",
    "patient_publication_items",
)


def _tenant_policy(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f"""
        CREATE POLICY clinic_isolation ON "{table}"
        USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        """
    )


def _tenant_id() -> sa.Column:
    return sa.Column(
        "clinic_id",
        postgresql.UUID(),
        sa.ForeignKey("clinics.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.add_column("importance_feedback_events", sa.Column("reason", sa.String(40)))
    op.add_column("importance_impressions", sa.Column("view_event_id", sa.String(120)))
    op.add_column(
        "importance_impressions",
        sa.Column("exposure_probability", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column(
        "importance_impressions",
        sa.Column("visible_ratio", sa.Float(), nullable=False, server_default="0.5"),
    )
    op.add_column(
        "importance_impressions",
        sa.Column("visible_duration_ms", sa.Integer(), nullable=False, server_default="2000"),
    )
    op.execute(
        "UPDATE importance_impressions SET view_event_id = 'legacy:' || id::text "
        "WHERE view_event_id IS NULL"
    )
    op.alter_column("importance_impressions", "view_event_id", nullable=False)
    op.create_unique_constraint(
        "uq_importance_impression_view_event",
        "importance_impressions",
        ["clinic_id", "view_event_id"],
    )

    op.add_column("decision_assessments", sa.Column("assertion_id", postgresql.UUID()))
    op.add_column(
        "decision_assessments",
        sa.Column("deterministic_floor", sa.String(20), nullable=False, server_default="standard"),
    )
    op.add_column("decision_assessments", sa.Column("model_risk", sa.String(20)))
    op.add_column(
        "decision_assessments",
        sa.Column("effective_risk", sa.String(20), nullable=False, server_default="standard"),
    )
    op.add_column("decision_assessments", sa.Column("confidence_lower_bound", sa.Float()))
    op.add_column(
        "decision_assessments", sa.Column("calibration_report_id", postgresql.UUID())
    )
    op.create_unique_constraint(
        "uq_decision_assessment_clinic_id", "decision_assessments", ["clinic_id", "id"]
    )
    op.execute(
        "UPDATE decision_assessments SET deterministic_floor = risk_tier, "
        "effective_risk = risk_tier, risk_rule_version = 'clinical-risk-rules-v2'"
    )

    op.create_table(
        "clinical_fact_assertions",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("entry_id", postgresql.UUID(), nullable=False),
        sa.Column("source_entry_version_id", postgresql.UUID(), nullable=False),
        sa.Column("provenance_pointer_id", postgresql.UUID(), nullable=False),
        sa.Column("highlight_id", postgresql.UUID()),
        sa.Column("fact_type", sa.String(80), nullable=False),
        sa.Column("subject_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("normalized_value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("normalized_key_hash", sa.String(64), nullable=False),
        sa.Column("polarity", sa.String(20), nullable=False, server_default="present"),
        sa.Column("clinical_status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("effective_time", sa.DateTime(timezone=True)),
        sa.Column("medication_ciphertext", sa.LargeBinary()),
        sa.Column("dose_value", sa.Float()),
        sa.Column("dose_unit", sa.String(20)),
        sa.Column("route", sa.String(40)),
        sa.Column("frequency", sa.String(40)),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("clinic_id", "id", name="uq_fact_assertion_clinic_id"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"],
            name="fk_fact_assertion_patient", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "entry_id"], ["entries.clinic_id", "entries.id"],
            name="fk_fact_assertion_entry", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "source_entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_fact_assertion_version"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "provenance_pointer_id"],
            ["provenance_pointers.clinic_id", "provenance_pointers.id"],
            name="fk_fact_assertion_pointer"
        ),
    )
    op.create_index(
        "ix_fact_assertion_patient_type", "clinical_fact_assertions",
        ["clinic_id", "patient_id", "fact_type"]
    )
    op.add_column("conflict_cases", sa.Column("left_assertion_id", postgresql.UUID()))
    op.add_column("conflict_cases", sa.Column("right_assertion_id", postgresql.UUID()))
    op.create_foreign_key(
        "fk_conflict_left_assertion", "conflict_cases", "clinical_fact_assertions",
        ["clinic_id", "left_assertion_id"], ["clinic_id", "id"]
    )
    op.create_foreign_key(
        "fk_conflict_right_assertion", "conflict_cases", "clinical_fact_assertions",
        ["clinic_id", "right_assertion_id"], ["clinic_id", "id"]
    )
    op.create_index(
        "ix_clinicalfactassertion_normalized_key_hash", "clinical_fact_assertions",
        ["normalized_key_hash"]
    )

    op.create_table(
        "evaluation_runs",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("exact_model_id", sa.String(160), nullable=False),
        sa.Column("task", sa.String(80), nullable=False),
        sa.Column("request_parameters_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("dataset_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("code_commit", sa.String(64), nullable=False),
        sa.Column("calibration_split", sa.String(100), nullable=False),
        sa.Column("holdout_split", sa.String(100), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("metrics_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("clinic_id", "id", name="uq_evaluation_run_clinic_id"),
    )
    op.create_index(
        "ix_evaluation_run_task_created", "evaluation_runs", ["clinic_id", "task", "created_at"]
    )

    op.create_table(
        "calibration_reports",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("evaluation_run_id", postgresql.UUID(), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("exact_model_id", sa.String(160), nullable=False),
        sa.Column("task", sa.String(80), nullable=False),
        sa.Column("request_parameters_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("code_commit", sa.String(64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("consultation_count", sa.Integer(), nullable=False),
        sa.Column("confidence_band", sa.String(20), nullable=False, server_default="unavailable"),
        sa.Column("accuracy_lower_bound", sa.Float()),
        sa.Column("metrics_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["clinic_id", "evaluation_run_id"], ["evaluation_runs.clinic_id", "evaluation_runs.id"],
            name="fk_calibration_report_run", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("clinic_id", "id", name="uq_calibration_report_clinic_id"),
    )
    op.create_index(
        "ix_calibration_report_lookup", "calibration_reports",
        ["clinic_id", "provider", "exact_model_id", "task", "expires_at"]
    )

    op.create_table(
        "calibration_buckets",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("calibration_report_id", postgresql.UUID(), nullable=False),
        sa.Column("bucket_key", sa.String(120), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("consultation_count", sa.Integer(), nullable=False),
        sa.Column("estimated_accuracy", sa.Float()),
        sa.Column("accuracy_lower_bound", sa.Float()),
        sa.Column("confidence_band", sa.String(20), nullable=False, server_default="unavailable"),
        sa.Column("metrics_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "calibration_report_id"],
            ["calibration_reports.clinic_id", "calibration_reports.id"],
            name="fk_calibration_bucket_report", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "clinic_id", "calibration_report_id", "bucket_key", name="uq_calibration_bucket"
        ),
    )

    op.create_table(
        "redaction_evaluation_runs",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("redactor_version", sa.String(80), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("phi_recall", sa.Float(), nullable=False),
        sa.Column("residual_phi_count", sa.Integer(), nullable=False),
        sa.Column("clinical_span_damage_count", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metrics_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_redaction_eval_version", "redaction_evaluation_runs",
        ["clinic_id", "redactor_version", "created_at"]
    )

    op.create_table(
        "patient_sharing_requests",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("entry_id", postgresql.UUID(), nullable=False),
        sa.Column("entry_version_id", postgresql.UUID(), nullable=False),
        sa.Column("requested_by_membership_id", postgresql.UUID(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reviewed_by_membership_id", postgresql.UUID()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"], ["patients.clinic_id", "patients.id"],
            name="fk_patient_sharing_request_patient", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_patient_sharing_request_version"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "requested_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_sharing_request_membership"
        ),
    )
    op.create_index(
        "ix_patient_sharing_request_status", "patient_sharing_requests",
        ["clinic_id", "patient_id", "status"]
    )

    op.create_unique_constraint(
        "uq_patient_publication_clinic_id", "patient_publications", ["clinic_id", "id"]
    )

    op.create_table(
        "patient_publication_items",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("publication_id", postgresql.UUID(), nullable=False),
        sa.Column("assertion_id", postgresql.UUID()),
        sa.Column("provenance_pointer_id", postgresql.UUID(), nullable=False),
        sa.Column("decision_assessment_id", postgresql.UUID()),
        sa.Column("support_state", sa.String(30), nullable=False),
        sa.Column("confidence_band", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["clinic_id", "publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_patient_publication_item_publication", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "provenance_pointer_id"],
            ["provenance_pointers.clinic_id", "provenance_pointers.id"],
            name="fk_patient_publication_item_pointer"
        ),
        sa.UniqueConstraint(
            "clinic_id", "publication_id", "provenance_pointer_id",
            name="uq_patient_publication_item_pointer"
        ),
    )

    op.create_foreign_key(
        "fk_decision_assessment_assertion", "decision_assessments", "clinical_fact_assertions",
        ["clinic_id", "assertion_id"], ["clinic_id", "id"]
    )
    op.create_foreign_key(
        "fk_decision_assessment_calibration", "decision_assessments", "calibration_reports",
        ["clinic_id", "calibration_report_id"], ["clinic_id", "id"]
    )
    op.create_foreign_key(
        "fk_publication_item_assertion", "patient_publication_items", "clinical_fact_assertions",
        ["clinic_id", "assertion_id"], ["clinic_id", "id"]
    )
    op.create_foreign_key(
        "fk_publication_item_assessment", "patient_publication_items", "decision_assessments",
        ["clinic_id", "decision_assessment_id"], ["clinic_id", "id"]
    )

    for table in NEW_TENANT_TABLES:
        _tenant_policy(table)


def downgrade() -> None:
    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f'DROP POLICY IF EXISTS clinic_isolation ON "{table}"')
    op.drop_constraint("fk_publication_item_assessment", "patient_publication_items", type_="foreignkey")
    op.drop_constraint("fk_publication_item_assertion", "patient_publication_items", type_="foreignkey")
    op.drop_constraint("fk_decision_assessment_calibration", "decision_assessments", type_="foreignkey")
    op.drop_constraint("fk_decision_assessment_assertion", "decision_assessments", type_="foreignkey")
    op.drop_constraint("fk_conflict_right_assertion", "conflict_cases", type_="foreignkey")
    op.drop_constraint("fk_conflict_left_assertion", "conflict_cases", type_="foreignkey")
    op.drop_column("conflict_cases", "right_assertion_id")
    op.drop_column("conflict_cases", "left_assertion_id")
    for table in (
        "patient_publication_items", "patient_sharing_requests", "redaction_evaluation_runs",
        "calibration_buckets", "calibration_reports", "evaluation_runs", "clinical_fact_assertions"
    ):
        op.drop_table(table)
    op.drop_constraint(
        "uq_patient_publication_clinic_id", "patient_publications", type_="unique"
    )
    for column in (
        "calibration_report_id", "confidence_lower_bound", "effective_risk",
        "model_risk", "deterministic_floor", "assertion_id"
    ):
        op.drop_column("decision_assessments", column)
    op.drop_constraint(
        "uq_decision_assessment_clinic_id", "decision_assessments", type_="unique"
    )
    op.drop_constraint(
        "uq_importance_impression_view_event", "importance_impressions", type_="unique"
    )
    for column in (
        "visible_duration_ms", "visible_ratio", "exposure_probability", "view_event_id"
    ):
        op.drop_column("importance_impressions", column)
    op.drop_column("importance_feedback_events", "reason")
