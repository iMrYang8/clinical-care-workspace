"""Fence worker writes, bind feedback requests, and serialize decay holds.

Revision ID: b5e7a9c2d140
Revises: a4c19d7e5b20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e7a9c2d140"
down_revision: str | None = "a4c19d7e5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "nightingale_app"
APPEND_ONLY_TABLES = (
    "audit_events",
    "domain_events",
    "provenance_pointers",
    "redaction_runs",
    "ai_runs",
    "importance_feedback_events",
    "archive_blobs",
)


def upgrade() -> None:
    legacy_hash = "0" * 64
    op.add_column(
        "importance_feedback_events",
        sa.Column(
            "request_sha256",
            sa.String(length=64),
            nullable=False,
            server_default=legacy_hash,
        ),
    )
    op.alter_column(
        "importance_feedback_events", "request_sha256", server_default=None
    )

    op.create_check_constraint(
        "ck_ai_run_interaction_type",
        "ai_runs",
        "interaction_type IN ('care_note', 'doctor_consult', "
        "'patient_insight', 'voice_session')",
    )

    op.drop_constraint("fk_entry_source_job_tenant", "entries", type_="foreignkey")
    op.create_unique_constraint(
        "uq_job_clinic_id_patient", "jobs", ["clinic_id", "id", "patient_id"]
    )
    op.create_foreign_key(
        "fk_entry_source_job_patient_tenant",
        "entries",
        "jobs",
        ["clinic_id", "source_job_id", "patient_id"],
        ["clinic_id", "id", "patient_id"],
    )

    # Retention-lock mutations and archive_version() take the same transaction
    # advisory lock. This closes the only protection relation that cannot be
    # serialized by an ordinary FK row lock because it is polymorphic.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nightingale_lock_decay_subject()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          old_key BIGINT;
          new_key BIGINT;
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            old_key := hashtextextended(
              'nightingale-decay:' || OLD.entity_id::text, 0
            );
          END IF;
          IF TG_OP <> 'DELETE' THEN
            new_key := hashtextextended(
              'nightingale-decay:' || NEW.entity_id::text, 0
            );
          END IF;

          IF old_key IS NOT NULL AND new_key IS NOT NULL
             AND old_key IS DISTINCT FROM new_key THEN
            PERFORM pg_advisory_xact_lock(LEAST(old_key, new_key));
            PERFORM pg_advisory_xact_lock(GREATEST(old_key, new_key));
          ELSIF COALESCE(new_key, old_key) IS NOT NULL THEN
            PERFORM pg_advisory_xact_lock(COALESCE(new_key, old_key));
          END IF;

          IF TG_OP <> 'DELETE' AND (
            (NEW.entity_type = 'entry_version' AND EXISTS (
              SELECT 1 FROM entry_versions
              WHERE clinic_id = NEW.clinic_id
                AND id = NEW.entity_id
                AND storage_tier = 'cold'
            ))
            OR
            (NEW.entity_type = 'entry' AND EXISTS (
              SELECT 1 FROM entry_versions
              WHERE clinic_id = NEW.clinic_id
                AND entry_id = NEW.entity_id
                AND storage_tier = 'cold'
            ))
          ) THEN
            RAISE EXCEPTION
              'retention lock requires rehydrated entry content'
              USING ERRCODE = '55000';
          END IF;
          RETURN COALESCE(NEW, OLD);
        END;
        $$;

        CREATE TRIGGER trg_retention_lock_decay_subject
        BEFORE INSERT OR UPDATE OR DELETE ON retention_locks
        FOR EACH ROW EXECUTE FUNCTION nightingale_lock_decay_subject();
        """
    )

    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"REVOKE UPDATE, DELETE ON TABLE {table} FROM {RUNTIME_ROLE}"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"GRANT UPDATE, DELETE ON TABLE {table} TO {RUNTIME_ROLE}")

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_retention_lock_decay_subject ON retention_locks;
        DROP FUNCTION IF EXISTS nightingale_lock_decay_subject();
        """
    )

    op.drop_constraint(
        "fk_entry_source_job_patient_tenant", "entries", type_="foreignkey"
    )
    op.drop_constraint("uq_job_clinic_id_patient", "jobs", type_="unique")
    op.create_foreign_key(
        "fk_entry_source_job_tenant",
        "entries",
        "jobs",
        ["clinic_id", "source_job_id"],
        ["clinic_id", "id"],
    )
    op.drop_constraint("ck_ai_run_interaction_type", "ai_runs", type_="check")
    op.drop_column("importance_feedback_events", "request_sha256")
