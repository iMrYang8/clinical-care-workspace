"""Harden tenant foreign keys and immutable publication provenance.

Revision ID: b42c8fbd91aa
Revises: a91e6c243b40
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b42c8fbd91aa"
down_revision: str | None = "a91e6c243b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entry_versions",
        sa.Column(
            "patient_facing", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.execute(
        """
        UPDATE entry_versions AS version
        SET patient_facing = entry.patient_facing
        FROM entries AS entry
        WHERE version.clinic_id = entry.clinic_id
          AND version.id = entry.current_version_id
        """
    )
    op.alter_column("entry_versions", "patient_facing", server_default=None)
    op.create_index(
        "ix_entry_versions_patient_facing", "entry_versions", ["patient_facing"]
    )

    op.create_unique_constraint(
        "uq_membership_clinic_id", "clinic_memberships", ["clinic_id", "id"]
    )

    op.drop_constraint(
        "fk_version_reverted_from", "entry_versions", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_version_reverted_from_tenant",
        "entry_versions",
        "entry_versions",
        ["clinic_id", "reverted_from_version_id"],
        ["clinic_id", "id"],
    )

    op.drop_constraint("fk_comment_parent", "comments", type_="foreignkey")
    op.drop_constraint(
        "comments_assigned_membership_id_fkey", "comments", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_comment_parent_tenant",
        "comments",
        "comments",
        ["clinic_id", "parent_id"],
        ["clinic_id", "id"],
    )
    op.create_foreign_key(
        "fk_comment_assignment_tenant",
        "comments",
        "clinic_memberships",
        ["clinic_id", "assigned_membership_id"],
        ["clinic_id", "id"],
    )

    op.drop_constraint(
        "comment_mentions_comment_id_fkey", "comment_mentions", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_comment_mention_tenant",
        "comment_mentions",
        "comments",
        ["clinic_id", "comment_id"],
        ["clinic_id", "id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "care_tasks_comment_id_fkey", "care_tasks", type_="foreignkey"
    )
    op.drop_constraint(
        "care_tasks_assignee_membership_id_fkey", "care_tasks", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_task_comment_tenant",
        "care_tasks",
        "comments",
        ["clinic_id", "comment_id"],
        ["clinic_id", "id"],
    )
    op.create_foreign_key(
        "fk_task_assignee_tenant",
        "care_tasks",
        "clinic_memberships",
        ["clinic_id", "assignee_membership_id"],
        ["clinic_id", "id"],
    )

    op.drop_constraint(
        "provenance_pointers_highlight_id_fkey",
        "provenance_pointers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "provenance_pointers_comment_id_fkey",
        "provenance_pointers",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_provenance_highlight_tenant",
        "provenance_pointers",
        "highlights",
        ["clinic_id", "highlight_id"],
        ["clinic_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_provenance_comment_tenant",
        "provenance_pointers",
        "comments",
        ["clinic_id", "comment_id"],
        ["clinic_id", "id"],
        ondelete="CASCADE",
    )

    op.execute(
        """
        CREATE FUNCTION nightingale_entry_version_append_only()
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
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_entry_version_append_only
          BEFORE UPDATE OR DELETE ON entry_versions
          FOR EACH ROW EXECUTE FUNCTION nightingale_entry_version_append_only();

        CREATE FUNCTION nightingale_provenance_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'provenance_pointers are append-only' USING ERRCODE = '55000';
        END;
        $$;
        CREATE TRIGGER trg_provenance_append_only
          BEFORE UPDATE OR DELETE ON provenance_pointers
          FOR EACH ROW EXECUTE FUNCTION nightingale_provenance_append_only();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_provenance_append_only ON provenance_pointers;
        DROP FUNCTION IF EXISTS nightingale_provenance_append_only();
        DROP TRIGGER IF EXISTS trg_entry_version_append_only ON entry_versions;
        DROP FUNCTION IF EXISTS nightingale_entry_version_append_only();
        """
    )

    op.drop_constraint(
        "fk_provenance_comment_tenant", "provenance_pointers", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_provenance_highlight_tenant", "provenance_pointers", type_="foreignkey"
    )
    op.create_foreign_key(
        "provenance_pointers_comment_id_fkey",
        "provenance_pointers",
        "comments",
        ["comment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "provenance_pointers_highlight_id_fkey",
        "provenance_pointers",
        "highlights",
        ["highlight_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("fk_task_assignee_tenant", "care_tasks", type_="foreignkey")
    op.drop_constraint("fk_task_comment_tenant", "care_tasks", type_="foreignkey")
    op.create_foreign_key(
        "care_tasks_assignee_membership_id_fkey",
        "care_tasks",
        "clinic_memberships",
        ["assignee_membership_id"],
        ["id"],
    )
    op.create_foreign_key(
        "care_tasks_comment_id_fkey",
        "care_tasks",
        "comments",
        ["comment_id"],
        ["id"],
    )

    op.drop_constraint(
        "fk_comment_mention_tenant", "comment_mentions", type_="foreignkey"
    )
    op.create_foreign_key(
        "comment_mentions_comment_id_fkey",
        "comment_mentions",
        "comments",
        ["comment_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "fk_comment_assignment_tenant", "comments", type_="foreignkey"
    )
    op.drop_constraint("fk_comment_parent_tenant", "comments", type_="foreignkey")
    op.create_foreign_key(
        "comments_assigned_membership_id_fkey",
        "comments",
        "clinic_memberships",
        ["assigned_membership_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_comment_parent", "comments", "comments", ["parent_id"], ["id"]
    )

    op.drop_constraint(
        "fk_version_reverted_from_tenant", "entry_versions", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_version_reverted_from",
        "entry_versions",
        "entry_versions",
        ["reverted_from_version_id"],
        ["id"],
    )
    op.drop_constraint(
        "uq_membership_clinic_id", "clinic_memberships", type_="unique"
    )
    op.drop_index("ix_entry_versions_patient_facing", table_name="entry_versions")
    op.drop_column("entry_versions", "patient_facing")
