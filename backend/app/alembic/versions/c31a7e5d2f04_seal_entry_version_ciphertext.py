"""Seal immutable entry version ciphertext.

Revision ID: c31a7e5d2f04
Revises: b42c8fbd91aa
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c31a7e5d2f04"
down_revision: str | None = "b42c8fbd91aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_function(*, seal_ciphertext: bool) -> None:
    ciphertext_checks = """
             OR NEW.title_ciphertext IS DISTINCT FROM OLD.title_ciphertext
             OR NEW.content_ciphertext IS DISTINCT FROM OLD.content_ciphertext
    """ if seal_ciphertext else ""
    op.execute(
        f"""
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
             {ciphertext_checks}
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


def upgrade() -> None:
    _install_function(seal_ciphertext=True)


def downgrade() -> None:
    _install_function(seal_ciphertext=False)
