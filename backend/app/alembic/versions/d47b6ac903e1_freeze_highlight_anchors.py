"""Freeze highlight anchors and enforce entry-version consistency.

Revision ID: d47b6ac903e1
Revises: c31a7e5d2f04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d47b6ac903e1"
down_revision: str | None = "c31a7e5d2f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION nightingale_highlight_anchor_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'highlight anchors are append-only' USING ERRCODE = '55000';
          END IF;

          IF TG_OP = 'UPDATE' AND (
               NEW.id IS DISTINCT FROM OLD.id
            OR NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
            OR NEW.patient_id IS DISTINCT FROM OLD.patient_id
            OR NEW.entry_id IS DISTINCT FROM OLD.entry_id
            OR NEW.source_entry_version_id IS DISTINCT FROM OLD.source_entry_version_id
            OR NEW.label_ciphertext IS DISTINCT FROM OLD.label_ciphertext
            OR NEW.critical IS DISTINCT FROM OLD.critical
            OR NEW.patient_facing IS DISTINCT FROM OLD.patient_facing
            OR NEW.anchor_state IS DISTINCT FROM OLD.anchor_state
            OR NEW.review_required IS DISTINCT FROM OLD.review_required
            OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
          ) THEN
            RAISE EXCEPTION 'immutable highlight anchor changed' USING ERRCODE = '55000';
          END IF;

          IF NOT EXISTS (
            SELECT 1
            FROM entry_versions AS version
            JOIN entries AS entry
              ON entry.clinic_id = version.clinic_id
             AND entry.id = version.entry_id
            WHERE version.clinic_id = NEW.clinic_id
              AND version.id = NEW.source_entry_version_id
              AND entry.id = NEW.entry_id
              AND entry.patient_id = NEW.patient_id
          ) THEN
            RAISE EXCEPTION 'highlight source must match its entry and patient'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_highlight_anchor_guard
          BEFORE INSERT OR UPDATE OR DELETE ON highlights
          FOR EACH ROW EXECUTE FUNCTION nightingale_highlight_anchor_guard();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_highlight_anchor_guard ON highlights;
        DROP FUNCTION IF EXISTS nightingale_highlight_anchor_guard();
        """
    )
