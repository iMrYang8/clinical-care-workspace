"""Enforce safety-learning and owner-proof tenant isolation invariants.

Revision ID: e6f2b8c0d315
Revises: e5d1f7a9c204
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6f2b8c0d315"
down_revision: str | None = "e5d1f7a9c204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "nightingale_app"
NEW_FORMULARY_TABLES = (
    "clinic_formulary_versions",
    "clinic_formulary_concepts",
)
NEW_DIRECT_PATIENT_TABLES = ("importance_candidate_sets",)
NEW_NONPATIENT_TABLES = ("importance_exposure_qualification_reports",)
NEW_RLS_TABLES = (
    *NEW_FORMULARY_TABLES,
    *NEW_DIRECT_PATIENT_TABLES,
    *NEW_NONPATIENT_TABLES,
)


def _set_force_rls(*, force: bool) -> None:
    operation = "FORCE" if force else "NO FORCE"
    op.execute(
        f"""
        DO $migration$
        DECLARE
          tenant_table record;
        BEGIN
          FOR tenant_table IN
            SELECT DISTINCT column_info.table_name
            FROM information_schema.columns AS column_info
            JOIN information_schema.tables AS table_info
              ON table_info.table_schema = column_info.table_schema
             AND table_info.table_name = column_info.table_name
             AND table_info.table_type = 'BASE TABLE'
            WHERE column_info.table_schema = current_schema()
              AND column_info.column_name = 'clinic_id'
            ORDER BY column_info.table_name
          LOOP
            EXECUTE format(
              'ALTER TABLE %I ENABLE ROW LEVEL SECURITY',
              tenant_table.table_name
            );
            EXECUTE format(
              'ALTER TABLE %I {operation} ROW LEVEL SECURITY',
              tenant_table.table_name
            );
          END LOOP;
        END
        $migration$;
        """
    )


def upgrade() -> None:
    op.add_column(
        "patient_glance_snapshots",
        sa.Column(
            "importance_mode",
            sa.String(length=20),
            nullable=False,
            server_default="shadow",
        ),
    )
    op.add_column(
        "patient_glance_snapshots",
        sa.Column(
            "importance_qualification_report_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "patient_glance_snapshots",
        sa.Column(
            "importance_qualification_report_version",
            sa.String(length=80),
            nullable=True,
        ),
    )
    op.add_column(
        "patient_glance_snapshots",
        sa.Column(
            "importance_qualification_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_patient_glance_importance_mode",
        "patient_glance_snapshots",
        "importance_mode IN ('disabled','shadow','active')",
    )
    op.alter_column("patient_glance_snapshots", "importance_mode", server_default=None)
    op.create_table(
        "importance_candidate_sets",
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "viewer_membership_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("candidate_set_id", sa.String(length=120), nullable=False),
        sa.Column("total_candidate_count", sa.Integer(), nullable=False),
        sa.Column("current_priorities_candidate_count", sa.Integer(), nullable=False),
        sa.Column("clinical_review_candidate_count", sa.Integer(), nullable=False),
        sa.Column(
            "current_priorities_protected_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "current_priorities_ordinary_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "clinical_review_protected_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "clinical_review_ordinary_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("protected_candidate_count", sa.Integer(), nullable=False),
        sa.Column("ordinary_candidate_count", sa.Integer(), nullable=False),
        sa.Column("current_priorities_displayed_count", sa.Integer(), nullable=False),
        sa.Column("clinical_review_displayed_count", sa.Integer(), nullable=False),
        sa.Column("displayed_count", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_importance_candidate_set_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "viewer_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_candidate_set_viewer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id", "candidate_set_id", name="uq_importance_candidate_set"
        ),
    )
    op.create_index(
        "ix_importance_candidate_set_observed",
        "importance_candidate_sets",
        ["clinic_id", "observed_at"],
    )
    op.create_table(
        "importance_exposure_qualification_reports",
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_version", sa.String(length=80), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_candidate_set_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("telemetry_count", sa.Integer(), nullable=False),
        sa.Column("displayed_count", sa.Integer(), nullable=False),
        sa.Column("protected_candidate_count", sa.Integer(), nullable=False),
        sa.Column("protected_displayed_count", sa.Integer(), nullable=False),
        sa.Column("ordinary_candidate_count", sa.Integer(), nullable=False),
        sa.Column("ordinary_displayed_count", sa.Integer(), nullable=False),
        sa.Column("protected_recall", sa.Float(), nullable=False),
        sa.Column("ordinary_recall", sa.Float(), nullable=False),
        sa.Column("ordinary_exposure_rate", sa.Float(), nullable=False),
        sa.Column("missing_telemetry_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_telemetry_count", sa.Integer(), nullable=False),
        sa.Column(
            "surface_metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("qualified", sa.Boolean(), nullable=False),
        sa.Column(
            "qualification_reasons_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "generated_by_membership_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "window_end >= window_start AND expires_at > created_at",
            name="ck_importance_exposure_report_window",
        ),
        sa.CheckConstraint(
            "source_candidate_set_count >= 0 "
            "AND candidate_count >= 0 AND telemetry_count >= 0 "
            "AND displayed_count >= 0 AND protected_candidate_count >= 0 "
            "AND protected_displayed_count >= 0 "
            "AND ordinary_candidate_count >= 0 "
            "AND ordinary_displayed_count >= 0 "
            "AND missing_telemetry_count >= 0 "
            "AND duplicate_telemetry_count >= 0",
            name="ck_importance_exposure_report_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "protected_recall >= 0 AND protected_recall <= 1 "
            "AND ordinary_recall >= 0 AND ordinary_recall <= 1 "
            "AND ordinary_exposure_rate >= 0 "
            "AND ordinary_exposure_rate <= 1",
            name="ck_importance_exposure_report_rates",
        ),
        sa.CheckConstraint(
            "NOT qualified OR (missing_telemetry_count = 0 "
            "AND duplicate_telemetry_count = 0 "
            "AND protected_candidate_count > 0 "
            "AND ordinary_candidate_count > 0 "
            "AND protected_recall = 1 "
            "AND ordinary_recall = 1 "
            "AND jsonb_array_length(qualification_reasons_json) = 0)",
            name="ck_importance_exposure_report_qualified",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "generated_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_exposure_report_generator",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_importance_exposure_report_current",
        "importance_exposure_qualification_reports",
        ["clinic_id", "created_at", "expires_at"],
    )
    op.add_column(
        "comments",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_comment_revision_positive", "comments", "revision >= 1"
    )
    # Existing deployments may already contain a learned negative allergy
    # adjustment. Repair it before installing the fail-closed database guard.
    op.execute(
        "UPDATE highlights SET learned_score = GREATEST(learned_score, 0) "
        "WHERE feature_keys_json @> '[\"entity:allergy\"]'::jsonb"
    )
    op.create_check_constraint(
        "ck_highlight_allergy_learning_floor",
        "highlights",
        "NOT (feature_keys_json @> '[\"entity:allergy\"]'::jsonb) "
        "OR learned_score >= 0",
    )
    # Service-side clamping is not a sufficient trust boundary. Repair any
    # legacy or non-finite value before installing the authoritative database
    # range check used by both shadow and active modes.
    op.execute(
        "UPDATE importance_feature_stats "
        "SET weight = CASE "
        "WHEN weight::text = 'NaN' THEN 0 "
        "WHEN weight::text = 'Infinity' THEN 0.20 "
        "WHEN weight::text = '-Infinity' THEN -0.20 "
        "ELSE GREATEST(-0.20, LEAST(0.20, weight)) END"
    )
    op.create_check_constraint(
        "ck_importance_feature_weight_bound",
        "importance_feature_stats",
        "weight >= -0.20 AND weight <= 0.20",
    )
    op.drop_constraint(
        "ck_importance_impression_rank", "importance_impressions", type_="check"
    )
    op.create_check_constraint(
        "ck_importance_impression_rank", "importance_impressions", "rank >= 1"
    )
    for table in NEW_RLS_TABLES:
        patient_expression = (
            "app_patient_context_allows(clinic_id, patient_id)"
            if table in NEW_DIRECT_PATIENT_TABLES
            else "app_nonpatient_context_allows(clinic_id)"
        )
        op.execute(
            f"""
            ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;
            CREATE POLICY clinic_isolation ON "{table}"
            USING (
              clinic_id = NULLIF(
                current_setting('app.current_clinic_id', true), ''
              )::uuid
            )
            WITH CHECK (
              clinic_id = NULLIF(
                current_setting('app.current_clinic_id', true), ''
              )::uuid
            );
            CREATE POLICY patient_scope ON "{table}" AS RESTRICTIVE
            USING ({patient_expression})
            WITH CHECK ({patient_expression});
            GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO {RUNTIME_ROLE};
            """
        )
    _set_force_rls(force=True)


def downgrade() -> None:
    _set_force_rls(force=False)
    for table in reversed(NEW_RLS_TABLES):
        op.execute(f'REVOKE ALL ON "{table}" FROM {RUNTIME_ROLE}')
        op.execute(f'DROP POLICY IF EXISTS patient_scope ON "{table}"')
        op.execute(f'DROP POLICY IF EXISTS clinic_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.drop_index(
        "ix_importance_exposure_report_current",
        table_name="importance_exposure_qualification_reports",
    )
    op.drop_table("importance_exposure_qualification_reports")
    op.drop_index(
        "ix_importance_candidate_set_observed",
        table_name="importance_candidate_sets",
    )
    op.drop_table("importance_candidate_sets")
    op.drop_constraint(
        "ck_importance_impression_rank", "importance_impressions", type_="check"
    )
    op.execute("UPDATE importance_impressions SET rank = LEAST(rank, 5)")
    op.create_check_constraint(
        "ck_importance_impression_rank",
        "importance_impressions",
        "rank BETWEEN 1 AND 5",
    )
    op.drop_constraint(
        "ck_highlight_allergy_learning_floor", "highlights", type_="check"
    )
    op.drop_constraint(
        "ck_importance_feature_weight_bound",
        "importance_feature_stats",
        type_="check",
    )
    op.drop_constraint("ck_comment_revision_positive", "comments", type_="check")
    op.drop_column("comments", "revision")
    op.drop_constraint(
        "ck_patient_glance_importance_mode",
        "patient_glance_snapshots",
        type_="check",
    )
    op.drop_column("patient_glance_snapshots", "importance_qualification_expires_at")
    op.drop_column(
        "patient_glance_snapshots", "importance_qualification_report_version"
    )
    op.drop_column("patient_glance_snapshots", "importance_qualification_report_id")
    op.drop_column("patient_glance_snapshots", "importance_mode")
