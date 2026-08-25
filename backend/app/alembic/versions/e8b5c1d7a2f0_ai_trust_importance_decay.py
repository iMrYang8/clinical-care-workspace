"""Add fail-closed AI jobs, bounded importance, and encrypted decay storage.

Revision ID: e8b5c1d7a2f0
Revises: d47b6ac903e1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8b5c1d7a2f0"
down_revision: str | None = "d47b6ac903e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "jobs",
    "job_attempts",
    "redaction_runs",
    "ai_runs",
    "importance_feedback_events",
    "importance_feature_stats",
    "archive_blobs",
    "decay_runs",
    "retention_locks",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobs (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL,
          kind VARCHAR(40) NOT NULL,
          state VARCHAR(30) NOT NULL DEFAULT 'pending',
          idempotency_key VARCHAR(200) NOT NULL,
          request_sha256 VARCHAR(64) NOT NULL,
          payload_ciphertext BYTEA NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 3,
          next_run_at TIMESTAMPTZ,
          locked_by VARCHAR(120),
          locked_until TIMESTAMPTZ,
          error_code VARCHAR(80),
          created_by_id UUID NOT NULL REFERENCES users(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_job_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_job_idempotency UNIQUE(clinic_id,kind,idempotency_key),
          CONSTRAINT fk_job_patient_tenant FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE
        );
        CREATE INDEX ix_job_clinic_state_next ON jobs(clinic_id,state,next_run_at);

        CREATE TABLE job_attempts (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          job_id UUID NOT NULL,
          attempt_no INTEGER NOT NULL CHECK(attempt_no >= 1),
          status VARCHAR(30) NOT NULL DEFAULT 'started',
          error_code VARCHAR(80),
          started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          completed_at TIMESTAMPTZ,
          CONSTRAINT uq_job_attempt_number UNIQUE(clinic_id,job_id,attempt_no),
          CONSTRAINT fk_job_attempt_tenant FOREIGN KEY(clinic_id,job_id)
            REFERENCES jobs(clinic_id,id) ON DELETE CASCADE
        );
        CREATE INDEX ix_job_attempt_job ON job_attempts(clinic_id,job_id);

        CREATE TABLE redaction_runs (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          source_entry_version_id UUID NOT NULL,
          status VARCHAR(30) NOT NULL,
          pipeline_version VARCHAR(80) NOT NULL DEFAULT 'nightingale-redaction-v1',
          input_sha256 VARCHAR(64) NOT NULL,
          redacted_sha256 VARCHAR(64) NOT NULL,
          entity_counts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          map_ciphertext BYTEA NOT NULL,
          residual_scan_passed BOOLEAN NOT NULL DEFAULT false,
          error_code VARCHAR(80),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_redaction_run_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT fk_redaction_source_version_tenant
            FOREIGN KEY(clinic_id,source_entry_version_id)
            REFERENCES entry_versions(clinic_id,id)
        );
        CREATE INDEX ix_redaction_source
          ON redaction_runs(clinic_id,source_entry_version_id);

        CREATE TABLE ai_runs (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL,
          job_id UUID NOT NULL,
          redaction_run_id UUID NOT NULL,
          source_entry_version_id UUID NOT NULL,
          interaction_type VARCHAR(60) NOT NULL,
          provider VARCHAR(60) NOT NULL,
          model VARCHAR(160) NOT NULL,
          status VARCHAR(30) NOT NULL,
          risk_tier VARCHAR(30) NOT NULL DEFAULT 'standard',
          fallback_reason VARCHAR(100),
          needs_review BOOLEAN NOT NULL DEFAULT false,
          request_sha256 VARCHAR(64) NOT NULL,
          output_entry_id UUID,
          output_entry_version_id UUID,
          warnings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          stale_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_ai_run_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_ai_run_job UNIQUE(clinic_id,job_id),
          CONSTRAINT fk_ai_run_patient_tenant FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_ai_run_job_tenant FOREIGN KEY(clinic_id,job_id)
            REFERENCES jobs(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_ai_run_redaction_tenant FOREIGN KEY(clinic_id,redaction_run_id)
            REFERENCES redaction_runs(clinic_id,id),
          CONSTRAINT fk_ai_run_source_version_tenant
            FOREIGN KEY(clinic_id,source_entry_version_id)
            REFERENCES entry_versions(clinic_id,id),
          CONSTRAINT fk_ai_run_output_entry_tenant FOREIGN KEY(clinic_id,output_entry_id)
            REFERENCES entries(clinic_id,id),
          CONSTRAINT fk_ai_run_output_version_tenant
            FOREIGN KEY(clinic_id,output_entry_version_id)
            REFERENCES entry_versions(clinic_id,id)
        );
        CREATE INDEX ix_ai_run_patient_created
          ON ai_runs(clinic_id,patient_id,created_at);

        CREATE TABLE importance_feedback_events (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          highlight_id UUID NOT NULL,
          actor_membership_id UUID NOT NULL,
          signal VARCHAR(30) NOT NULL,
          feature_keys_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          applied_delta DOUBLE PRECISION NOT NULL,
          idempotency_key VARCHAR(200) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_importance_feedback_idempotency UNIQUE(clinic_id,idempotency_key),
          CONSTRAINT fk_importance_feedback_highlight_tenant
            FOREIGN KEY(clinic_id,highlight_id)
            REFERENCES highlights(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_importance_feedback_actor_tenant
            FOREIGN KEY(clinic_id,actor_membership_id)
            REFERENCES clinic_memberships(clinic_id,id)
        );
        CREATE INDEX ix_importance_feedback_highlight
          ON importance_feedback_events(clinic_id,highlight_id);

        CREATE TABLE importance_feature_stats (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          feature_key VARCHAR(120) NOT NULL,
          weight DOUBLE PRECISION NOT NULL DEFAULT 0,
          positive_count INTEGER NOT NULL DEFAULT 0,
          negative_count INTEGER NOT NULL DEFAULT 0,
          observation_count INTEGER NOT NULL DEFAULT 0,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_importance_feature_stat UNIQUE(clinic_id,feature_key)
        );
        CREATE INDEX ix_importance_feature_clinic
          ON importance_feature_stats(clinic_id,feature_key);

        CREATE TABLE archive_blobs (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          entry_version_id UUID NOT NULL,
          compression VARCHAR(20) NOT NULL DEFAULT 'zstd',
          encryption VARCHAR(30) NOT NULL DEFAULT 'aes-256-gcm',
          key_id VARCHAR(80) NOT NULL DEFAULT 'field-master-v1',
          payload_ciphertext BYTEA NOT NULL,
          plaintext_sha256 VARCHAR(64) NOT NULL,
          ciphertext_sha256 VARCHAR(64) NOT NULL,
          original_size INTEGER NOT NULL,
          compressed_size INTEGER NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_archive_blob_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_archive_entry_version UNIQUE(clinic_id,entry_version_id),
          CONSTRAINT fk_archive_entry_version_tenant
            FOREIGN KEY(clinic_id,entry_version_id)
            REFERENCES entry_versions(clinic_id,id)
        );

        CREATE TABLE decay_runs (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          policy_version VARCHAR(80) NOT NULL DEFAULT 'nightingale-decay-v1',
          cutoff_at TIMESTAMPTZ NOT NULL,
          dry_run BOOLEAN NOT NULL DEFAULT true,
          candidate_count INTEGER NOT NULL DEFAULT 0,
          archived_count INTEGER NOT NULL DEFAULT 0,
          error_count INTEGER NOT NULL DEFAULT 0,
          created_by_id UUID NOT NULL REFERENCES users(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_decay_run_clinic_created ON decay_runs(clinic_id,created_at);

        CREATE TABLE retention_locks (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          entity_type VARCHAR(40) NOT NULL,
          entity_id UUID NOT NULL,
          reason_code VARCHAR(80) NOT NULL,
          locked_until TIMESTAMPTZ,
          created_by_id UUID NOT NULL REFERENCES users(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_retention_lock_entity UNIQUE(clinic_id,entity_type,entity_id)
        );
        CREATE INDEX ix_retention_lock_entity
          ON retention_locks(clinic_id,entity_type,entity_id);
        """
    )

    op.add_column(
        "highlights",
        sa.Column("feature_keys_json", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "highlights",
        sa.Column("base_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "highlights",
        sa.Column("learned_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "highlights",
        sa.Column("final_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "highlights",
        sa.Column("risk_reason", sa.String(length=100), nullable=False, server_default="recency"),
    )
    op.add_column(
        "highlights",
        sa.Column("unresolved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "highlights",
        sa.Column("clinician_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_highlights_final_score", "highlights", ["final_score"])
    op.create_foreign_key(
        "fk_entry_source_job_tenant",
        "entries",
        "jobs",
        ["clinic_id", "source_job_id"],
        ["clinic_id", "id"],
    )
    op.create_foreign_key(
        "fk_version_archive_blob_tenant",
        "entry_versions",
        "archive_blobs",
        ["clinic_id", "archive_blob_id"],
        ["clinic_id", "id"],
    )

    for table in TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f"""
            CREATE POLICY clinic_isolation ON "{table}"
            USING (
              clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
            )
            WITH CHECK (
              clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
            )
            """
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION nightingale_entry_version_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'entry_versions are append-only' USING ERRCODE = '55000';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
             OR NEW.entry_id IS DISTINCT FROM OLD.entry_id
             OR NEW.version_no IS DISTINCT FROM OLD.version_no
             OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
             OR NEW.patient_facing IS DISTINCT FROM OLD.patient_facing
             OR NEW.author_id IS DISTINCT FROM OLD.author_id
             OR NEW.reverted_from_version_id IS DISTINCT FROM OLD.reverted_from_version_id
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'immutable entry_version metadata changed'
              USING ERRCODE = '55000';
          END IF;

          IF NEW.title_ciphertext IS NOT DISTINCT FROM OLD.title_ciphertext
             AND NEW.content_ciphertext IS NOT DISTINCT FROM OLD.content_ciphertext
             AND NEW.storage_tier IS NOT DISTINCT FROM OLD.storage_tier
             AND NEW.archive_blob_id IS NOT DISTINCT FROM OLD.archive_blob_id THEN
            RETURN NEW;
          END IF;

          IF OLD.storage_tier IN ('hot','warm') AND NEW.storage_tier = 'cold'
             AND OLD.title_ciphertext IS NOT NULL AND OLD.content_ciphertext IS NOT NULL
             AND NEW.title_ciphertext IS NULL AND NEW.content_ciphertext IS NULL
             AND NEW.archive_blob_id IS NOT NULL THEN
            RETURN NEW;
          END IF;

          IF OLD.storage_tier = 'cold' AND NEW.storage_tier IN ('hot','warm')
             AND OLD.title_ciphertext IS NULL AND OLD.content_ciphertext IS NULL
             AND NEW.title_ciphertext IS NOT NULL AND NEW.content_ciphertext IS NOT NULL
             AND NEW.archive_blob_id IS NOT DISTINCT FROM OLD.archive_blob_id THEN
            RETURN NEW;
          END IF;

          IF OLD.storage_tier = 'hot' AND NEW.storage_tier = 'warm'
             AND NEW.title_ciphertext IS NOT DISTINCT FROM OLD.title_ciphertext
             AND NEW.content_ciphertext IS NOT DISTINCT FROM OLD.content_ciphertext
             AND NEW.archive_blob_id IS NOT DISTINCT FROM OLD.archive_blob_id THEN
            RETURN NEW;
          END IF;

          RAISE EXCEPTION 'immutable entry_version payload or invalid physical storage transition'
            USING ERRCODE = '55000';
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nightingale_entry_version_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'entry_versions are append-only' USING ERRCODE = '55000';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
             OR NEW.entry_id IS DISTINCT FROM OLD.entry_id
             OR NEW.version_no IS DISTINCT FROM OLD.version_no
             OR NEW.title_ciphertext IS DISTINCT FROM OLD.title_ciphertext
             OR NEW.content_ciphertext IS DISTINCT FROM OLD.content_ciphertext
             OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
             OR NEW.patient_facing IS DISTINCT FROM OLD.patient_facing
             OR NEW.author_id IS DISTINCT FROM OLD.author_id
             OR NEW.reverted_from_version_id IS DISTINCT FROM OLD.reverted_from_version_id
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'immutable entry_version payload or metadata changed'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        """
    )
    # This revision owns the only durable copy of a cold payload. Destructive
    # downgrade is therefore forbidden until every version has been rehydrated
    # and authenticated by the application. Rehydration intentionally retains
    # archive_blob_id for auditability, so clear those verified references
    # before dropping the blob table; otherwise a later upgrade cannot restore
    # the FK against an empty archive table.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM entry_versions
            WHERE storage_tier = 'cold'
               OR title_ciphertext IS NULL
               OR content_ciphertext IS NULL
          ) THEN
            RAISE EXCEPTION
              'AI trust downgrade blocked: rehydrate every cold entry version first'
              USING ERRCODE = '55000';
          END IF;

          UPDATE entry_versions
          SET archive_blob_id = NULL
          WHERE archive_blob_id IS NOT NULL;
        END;
        $$;
        """
    )
    op.drop_constraint("fk_version_archive_blob_tenant", "entry_versions", type_="foreignkey")
    op.drop_constraint("fk_entry_source_job_tenant", "entries", type_="foreignkey")
    op.drop_index("ix_highlights_final_score", table_name="highlights")
    for column in (
        "clinician_confirmed",
        "unresolved",
        "risk_reason",
        "final_score",
        "learned_score",
        "base_score",
        "feature_keys_json",
    ):
        op.drop_column("highlights", column)
    for table in reversed(TENANT_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
