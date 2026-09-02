"""Add clinic formulary versions and overlapping-speaker evidence.

Revision ID: e5d1f7a9c204
Revises: e4c9d65f2a13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5d1f7a9c204"
down_revision: str | None = "e4c9d65f2a13"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "clinic_operational_settings",
        sa.Column(
            "formulary_template",
            sa.String(length=80),
            nullable=False,
            server_default="nightingale-clinic-formulary-v1",
        ),
    )
    op.alter_column(
        "clinic_operational_settings", "formulary_template", server_default=None
    )
    op.add_column(
        "highlights",
        sa.Column("candidate_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_highlight_candidate_fingerprint_sha256",
        "highlights",
        "candidate_fingerprint IS NULL OR candidate_fingerprint ~ '^[0-9a-f]{64}$'",
    )
    op.create_unique_constraint(
        "uq_highlight_candidate_fingerprint",
        "highlights",
        ["clinic_id", "candidate_fingerprint"],
    )
    op.create_index(
        "ix_highlight_candidate_fingerprint",
        "highlights",
        ["clinic_id", "candidate_fingerprint"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nightingale_highlight_candidate_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.candidate_fingerprint IS DISTINCT FROM OLD.candidate_fingerprint THEN
            RAISE EXCEPTION 'immutable highlight candidate fingerprint changed'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_highlight_candidate_fingerprint_guard
          BEFORE UPDATE ON highlights
          FOR EACH ROW EXECUTE FUNCTION nightingale_highlight_candidate_guard();
        """
    )
    op.create_table(
        "clinic_formulary_versions",
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_code", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by_membership_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("content_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "qualified_by_membership_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("qualification_source", sa.String(length=30), nullable=True),
        sa.Column(
            "activated_by_membership_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','active','retired')",
            name="ck_clinic_formulary_version_status",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_formulary_creator",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "qualified_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_formulary_qualifier",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "activated_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_formulary_activator",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status != 'active' OR qualified_at IS NOT NULL",
            name="ck_clinic_formulary_active_qualified",
        ),
        sa.CheckConstraint(
            "qualification_source IS NULL OR "
            "qualification_source IN ('clinic_admin','platform_template')",
            name="ck_clinic_formulary_qualification_source",
        ),
        sa.CheckConstraint(
            "(qualified_at IS NULL AND qualified_by_membership_id IS NULL "
            "AND qualification_source IS NULL) OR "
            "(qualified_at IS NOT NULL AND qualification_source = 'clinic_admin' "
            "AND qualified_by_membership_id IS NOT NULL) OR "
            "(qualified_at IS NOT NULL AND qualification_source = 'platform_template' "
            "AND qualified_by_membership_id IS NULL)",
            name="ck_clinic_formulary_qualification_actor",
        ),
        sa.UniqueConstraint("clinic_id", "id", name="uq_clinic_formulary_version_id"),
        sa.UniqueConstraint(
            "clinic_id",
            "version_code",
            name="uq_clinic_formulary_version_code",
        ),
    )
    op.create_index(
        "ix_clinic_formulary_active",
        "clinic_formulary_versions",
        ["clinic_id", "status", "effective_at"],
    )
    op.create_index(
        "uq_clinic_formulary_one_active",
        "clinic_formulary_versions",
        ["clinic_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "clinic_formulary_concepts",
        sa.Column("clinic_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "formulary_version_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("concept_code", sa.String(length=100), nullable=False),
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column(
            "multilingual_aliases_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("dose_unit", sa.String(length=20), nullable=False),
        sa.Column("minimum_single_dose", sa.Float(), nullable=False),
        sa.Column("maximum_single_dose", sa.Float(), nullable=False),
        sa.Column(
            "permitted_routes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "contraindicated_allergy_concepts_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "minimum_single_dose > 0 AND maximum_single_dose >= minimum_single_dose",
            name="ck_clinic_formulary_dose_range",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "formulary_version_id"],
            ["clinic_formulary_versions.clinic_id", "clinic_formulary_versions.id"],
            name="fk_clinic_formulary_concept_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "formulary_version_id",
            "concept_code",
            name="uq_clinic_formulary_concept",
        ),
    )
    op.create_index(
        "ix_clinic_formulary_concept_version",
        "clinic_formulary_concepts",
        ["clinic_id", "formulary_version_id"],
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nightingale_formulary_version_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'formulary versions are immutable'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
             OR NEW.id IS DISTINCT FROM OLD.id
             OR NEW.version_code IS DISTINCT FROM OLD.version_code
             OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
             OR NEW.created_by_membership_id IS DISTINCT FROM OLD.created_by_membership_id
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'immutable formulary version content changed'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.content_locked_at IS NOT NULL
             AND NEW.content_locked_at IS DISTINCT FROM OLD.content_locked_at THEN
            RAISE EXCEPTION 'formulary content lock is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.qualified_at IS NOT NULL
             AND (NEW.qualified_at IS DISTINCT FROM OLD.qualified_at
                  OR NEW.qualified_by_membership_id IS DISTINCT FROM OLD.qualified_by_membership_id
                  OR NEW.qualification_source IS DISTINCT FROM OLD.qualification_source) THEN
            RAISE EXCEPTION 'formulary qualification is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.status IS DISTINCT FROM OLD.status
             AND NOT (
               (OLD.status = 'draft' AND NEW.status IN ('active','retired'))
               OR (OLD.status = 'active' AND NEW.status = 'retired')
             ) THEN
            RAISE EXCEPTION 'invalid formulary status transition'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.effective_at IS DISTINCT FROM OLD.effective_at
             AND NOT (OLD.status = 'draft' AND NEW.status = 'active') THEN
            RAISE EXCEPTION 'formulary effective time is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.activated_by_membership_id IS NOT NULL
             AND NEW.activated_by_membership_id IS DISTINCT FROM OLD.activated_by_membership_id THEN
            RAISE EXCEPTION 'formulary activation actor is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.retired_at IS NOT NULL
             AND NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
            RAISE EXCEPTION 'formulary retirement time is immutable'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_clinic_formulary_version_guard
          BEFORE UPDATE OR DELETE ON clinic_formulary_versions
          FOR EACH ROW EXECUTE FUNCTION nightingale_formulary_version_guard();

        CREATE OR REPLACE FUNCTION nightingale_formulary_concept_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
          locked_at timestamptz;
        BEGIN
          IF TG_OP IN ('UPDATE', 'DELETE') THEN
            RAISE EXCEPTION 'formulary concepts are immutable'
              USING ERRCODE = '55000';
          END IF;
          SELECT content_locked_at INTO locked_at
            FROM clinic_formulary_versions
           WHERE clinic_id = NEW.clinic_id AND id = NEW.formulary_version_id;
          IF locked_at IS NOT NULL THEN
            RAISE EXCEPTION 'formulary content is locked'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_clinic_formulary_concept_guard
          BEFORE INSERT OR UPDATE OR DELETE ON clinic_formulary_concepts
          FOR EACH ROW EXECUTE FUNCTION nightingale_formulary_concept_guard();
        """
    )
    op.add_column(
        "transcript_segments",
        sa.Column(
            "speaker_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("transcript_segments", "speaker_ids_json", server_default=None)
    op.add_column(
        "transcript_segments",
        sa.Column(
            "language_spans_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_transcript_segment_language_spans_array",
        "transcript_segments",
        "jsonb_typeof(language_spans_json) = 'array'",
    )
    op.alter_column("transcript_segments", "language_spans_json", server_default=None)

    # Category is intentionally explicit and nullable.  The encrypted wording
    # of legacy specific-substance assertions is not decrypted during schema
    # migration, so those remain unavailable until their source is reviewed.
    # The assertion scope itself is sufficient to classify broad NKDA rows.
    op.add_column(
        "clinical_fact_assertions",
        sa.Column("allergy_category", sa.String(length=20), nullable=True),
    )
    op.execute(
        "UPDATE clinical_fact_assertions SET allergy_category = 'drug' "
        "WHERE fact_type = 'allergy' AND assertion_scope = 'drug_allergies'"
    )
    op.create_check_constraint(
        "ck_fact_assertion_allergy_category",
        "clinical_fact_assertions",
        "allergy_category IS NULL OR "
        "(fact_type = 'allergy' AND "
        "allergy_category IN ('drug','food','environmental'))",
    )
    op.add_column(
        "clinical_fact_assertions",
        sa.Column("source_role", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "clinical_fact_assertions",
        sa.Column("source_section", sa.String(length=20), nullable=True),
    )
    op.execute(
        """
        UPDATE clinical_fact_assertions AS assertion
           SET source_section = entry.section,
               source_role = CASE
                 WHEN assertion.origin IN ('ai', 'system') THEN 'system'
                 WHEN entry.section = 'patient' THEN 'patient'
                 ELSE (
                   SELECT membership.role
                     FROM entry_versions AS version
                     JOIN clinic_memberships AS membership
                       ON membership.clinic_id = version.clinic_id
                      AND membership.user_id = version.author_id
                    WHERE version.clinic_id = assertion.clinic_id
                      AND version.id = assertion.source_entry_version_id
                    ORDER BY membership.created_at
                    LIMIT 1
                 )
               END
          FROM entries AS entry
         WHERE entry.clinic_id = assertion.clinic_id
           AND entry.id = assertion.entry_id
        """
    )

    # Persist each evaluation population explicitly. ``sample_count`` remains
    # as a compatibility projection and is constrained to the untouched
    # holdout population used by the Wilson lower bound.
    for table_name in ("evaluation_runs", "calibration_reports"):
        op.add_column(
            table_name,
            sa.Column(
                "total_sample_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.add_column(
            table_name,
            sa.Column(
                "calibration_sample_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.add_column(
            table_name,
            sa.Column(
                "holdout_sample_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.execute(
            f"""
            UPDATE {table_name}
               SET calibration_sample_count = CASE
                     WHEN COALESCE(
                       metrics_json ->> 'calibration_sample_count', ''
                     ) ~ '^[0-9]+$'
                     THEN (metrics_json ->> 'calibration_sample_count')::integer
                     ELSE 0
                   END,
                   holdout_sample_count = sample_count
            """
        )
        op.execute(
            f"""
            UPDATE {table_name}
               SET total_sample_count =
                   calibration_sample_count + holdout_sample_count
            """
        )
        constraint_name = (
            "ck_evaluation_run_sample_accounting"
            if table_name == "evaluation_runs"
            else "ck_calibration_report_sample_accounting"
        )
        op.create_check_constraint(
            constraint_name,
            table_name,
            "total_sample_count >= 0 AND calibration_sample_count >= 0 AND "
            "holdout_sample_count >= 0 AND sample_count = holdout_sample_count AND "
            "total_sample_count = calibration_sample_count + holdout_sample_count",
        )
        for column_name in (
            "total_sample_count",
            "calibration_sample_count",
            "holdout_sample_count",
        ):
            op.alter_column(table_name, column_name, server_default=None)
    op.execute(
        """
        UPDATE calibration_reports AS report
           SET total_sample_count = run.total_sample_count,
               calibration_sample_count = run.calibration_sample_count,
               holdout_sample_count = run.holdout_sample_count
          FROM evaluation_runs AS run
         WHERE run.clinic_id = report.clinic_id
           AND run.id = report.evaluation_run_id
           AND report.sample_count = run.holdout_sample_count
        """
    )


def downgrade() -> None:
    for table_name, constraint_name in (
        ("calibration_reports", "ck_calibration_report_sample_accounting"),
        ("evaluation_runs", "ck_evaluation_run_sample_accounting"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="check")
        op.drop_column(table_name, "holdout_sample_count")
        op.drop_column(table_name, "calibration_sample_count")
        op.drop_column(table_name, "total_sample_count")
    op.drop_constraint(
        "ck_fact_assertion_allergy_category",
        "clinical_fact_assertions",
        type_="check",
    )
    op.drop_column("clinical_fact_assertions", "allergy_category")
    op.drop_column("clinical_fact_assertions", "source_section")
    op.drop_column("clinical_fact_assertions", "source_role")
    op.drop_constraint(
        "ck_transcript_segment_language_spans_array",
        "transcript_segments",
        type_="check",
    )
    op.drop_column("transcript_segments", "language_spans_json")
    op.drop_column("transcript_segments", "speaker_ids_json")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_clinic_formulary_concept_guard "
        "ON clinic_formulary_concepts"
    )
    op.execute("DROP FUNCTION IF EXISTS nightingale_formulary_concept_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_clinic_formulary_version_guard "
        "ON clinic_formulary_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS nightingale_formulary_version_guard()")
    op.drop_index(
        "ix_clinic_formulary_concept_version",
        table_name="clinic_formulary_concepts",
    )
    op.drop_table("clinic_formulary_concepts")
    op.drop_index("ix_clinic_formulary_active", table_name="clinic_formulary_versions")
    op.execute("DROP INDEX IF EXISTS uq_clinic_formulary_one_active")
    op.drop_table("clinic_formulary_versions")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_highlight_candidate_fingerprint_guard ON highlights"
    )
    op.execute("DROP FUNCTION IF EXISTS nightingale_highlight_candidate_guard()")
    # IF EXISTS keeps downgrade usable for a development database that applied
    # an earlier uncommitted e5 before candidate fingerprints joined this same
    # revision during the hardening pass.
    op.execute("DROP INDEX IF EXISTS ix_highlight_candidate_fingerprint")
    op.execute(
        "ALTER TABLE highlights DROP CONSTRAINT IF EXISTS "
        "uq_highlight_candidate_fingerprint"
    )
    op.execute(
        "ALTER TABLE highlights DROP CONSTRAINT IF EXISTS "
        "ck_highlight_candidate_fingerprint_sha256"
    )
    op.execute("ALTER TABLE highlights DROP COLUMN IF EXISTS candidate_fingerprint")
    op.execute(
        "ALTER TABLE clinic_operational_settings "
        "DROP COLUMN IF EXISTS formulary_template"
    )
