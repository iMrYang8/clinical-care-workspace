"""Enforce patient sharing request and publication integrity.

Revision ID: d4e8a91c7b62
Revises: c6d9a8e24170
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e8a91c7b62"
down_revision: str | None = "c6d9a8e24170"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_entry_clinic_patient_id",
        "entries",
        ["clinic_id", "patient_id", "id"],
    )
    op.create_unique_constraint(
        "uq_entry_version_clinic_entry_id",
        "entry_versions",
        ["clinic_id", "entry_id", "id"],
    )

    op.add_column(
        "patient_publications",
        sa.Column("entry_id", postgresql.UUID(), nullable=True),
    )
    op.add_column(
        "patient_publications",
        sa.Column("supersedes_publication_id", postgresql.UUID(), nullable=True),
    )
    op.execute(
        """
        UPDATE patient_publications AS publication
        SET entry_id = version.entry_id
        FROM entry_versions AS version
        WHERE version.clinic_id = publication.clinic_id
          AND version.id = publication.entry_version_id
        """
    )
    op.alter_column("patient_publications", "entry_id", nullable=False)
    op.create_unique_constraint(
        "uq_patient_publication_scope_id",
        "patient_publications",
        ["clinic_id", "patient_id", "entry_id", "id"],
    )
    op.create_foreign_key(
        "fk_patient_publication_entry",
        "patient_publications",
        "entries",
        ["clinic_id", "patient_id", "entry_id"],
        ["clinic_id", "patient_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "fk_patient_publication_version",
        "patient_publications",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_patient_publication_version",
        "patient_publications",
        "entry_versions",
        ["clinic_id", "entry_id", "entry_version_id"],
        ["clinic_id", "entry_id", "id"],
    )

    # Preserve the latest active receipt and make every older active row an
    # explicit historical supersession before installing the partial unique.
    op.execute(
        """
        WITH ranked AS (
          SELECT id,
                 approved_at,
                 row_number() OVER (
                   PARTITION BY clinic_id, entry_id
                   ORDER BY approved_at DESC, id DESC
                 ) AS rank
          FROM patient_publications
          WHERE withdrawn_at IS NULL
        )
        UPDATE patient_publications AS publication
        SET withdrawn_at = ranked.approved_at
        FROM ranked
        WHERE publication.id = ranked.id
          AND ranked.rank > 1
        """
    )
    op.execute(
        """
        WITH ordered AS (
          SELECT id,
                 lag(id) OVER (
                   PARTITION BY clinic_id, entry_id
                   ORDER BY approved_at, id
                 ) AS previous_id
          FROM patient_publications
        )
        UPDATE patient_publications AS publication
        SET supersedes_publication_id = ordered.previous_id
        FROM ordered
        WHERE publication.id = ordered.id
          AND ordered.previous_id IS NOT NULL
        """
    )
    op.create_foreign_key(
        "fk_patient_publication_supersedes",
        "patient_publications",
        "patient_publications",
        ["clinic_id", "patient_id", "entry_id", "supersedes_publication_id"],
        ["clinic_id", "patient_id", "entry_id", "id"],
    )
    op.create_check_constraint(
        "ck_patient_publication_not_self_superseding",
        "patient_publications",
        "supersedes_publication_id IS NULL OR supersedes_publication_id <> id",
    )
    op.create_index(
        "uq_patient_publication_active_entry",
        "patient_publications",
        ["clinic_id", "entry_id"],
        unique=True,
        postgresql_where=sa.text("withdrawn_at IS NULL"),
    )

    op.add_column(
        "patient_sharing_requests",
        sa.Column("publication_id", postgresql.UUID(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_patient_sharing_request_clinic_id",
        "patient_sharing_requests",
        ["clinic_id", "id"],
    )
    op.create_foreign_key(
        "fk_patient_sharing_request_entry",
        "patient_sharing_requests",
        "entries",
        ["clinic_id", "patient_id", "entry_id"],
        ["clinic_id", "patient_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "fk_patient_sharing_request_version",
        "patient_sharing_requests",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_patient_sharing_request_version",
        "patient_sharing_requests",
        "entry_versions",
        ["clinic_id", "entry_id", "entry_version_id"],
        ["clinic_id", "entry_id", "id"],
    )
    op.create_foreign_key(
        "fk_patient_sharing_request_reviewer",
        "patient_sharing_requests",
        "clinic_memberships",
        ["clinic_id", "reviewed_by_membership_id"],
        ["clinic_id", "id"],
    )
    op.execute(
        """
        UPDATE patient_sharing_requests AS request
        SET publication_id = (
          SELECT publication.id
          FROM patient_publications AS publication
          WHERE publication.clinic_id = request.clinic_id
            AND publication.patient_id = request.patient_id
            AND publication.entry_id = request.entry_id
            AND publication.approved_at >= request.created_at
          ORDER BY publication.approved_at, publication.id
          LIMIT 1
        )
        WHERE request.status IN ('approved', 'withdrawn')
        """
    )
    op.execute(
        """
        UPDATE patient_sharing_requests
        SET status = 'superseded'
        WHERE status IN ('approved', 'withdrawn')
          AND publication_id IS NULL
        """
    )
    op.create_foreign_key(
        "fk_patient_sharing_request_publication",
        "patient_sharing_requests",
        "patient_publications",
        ["clinic_id", "patient_id", "entry_id", "publication_id"],
        ["clinic_id", "patient_id", "entry_id", "id"],
    )

    # Only the newest current-version request stays actionable. Historical
    # pending rows are retained with an explicit superseded state.
    op.execute(
        """
        UPDATE patient_sharing_requests AS request
        SET status = 'superseded'
        FROM entries AS entry
        WHERE request.clinic_id = entry.clinic_id
          AND request.entry_id = entry.id
          AND request.status = 'pending'
          AND request.entry_version_id <> entry.current_version_id
        """
    )
    op.execute(
        """
        WITH ranked AS (
          SELECT id,
                 row_number() OVER (
                   PARTITION BY clinic_id, entry_id
                   ORDER BY created_at DESC, id DESC
                 ) AS rank
          FROM patient_sharing_requests
          WHERE status = 'pending'
        )
        UPDATE patient_sharing_requests AS request
        SET status = 'superseded'
        FROM ranked
        WHERE request.id = ranked.id
          AND ranked.rank > 1
        """
    )
    op.create_check_constraint(
        "ck_patient_sharing_request_status",
        "patient_sharing_requests",
        "status IN ('pending','approved','rejected','superseded','withdrawn')",
    )
    op.create_index(
        "uq_patient_sharing_request_pending_entry",
        "patient_sharing_requests",
        ["clinic_id", "entry_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_patient_sharing_request_pending_entry",
        table_name="patient_sharing_requests",
    )
    op.drop_constraint(
        "ck_patient_sharing_request_status",
        "patient_sharing_requests",
        type_="check",
    )
    op.drop_constraint(
        "fk_patient_sharing_request_publication",
        "patient_sharing_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_patient_sharing_request_reviewer",
        "patient_sharing_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_patient_sharing_request_version",
        "patient_sharing_requests",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_patient_sharing_request_version",
        "patient_sharing_requests",
        "entry_versions",
        ["clinic_id", "entry_version_id"],
        ["clinic_id", "id"],
    )
    op.drop_constraint(
        "fk_patient_sharing_request_entry",
        "patient_sharing_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_patient_sharing_request_clinic_id",
        "patient_sharing_requests",
        type_="unique",
    )
    op.drop_column("patient_sharing_requests", "publication_id")

    op.drop_index(
        "uq_patient_publication_active_entry", table_name="patient_publications"
    )
    op.drop_constraint(
        "ck_patient_publication_not_self_superseding",
        "patient_publications",
        type_="check",
    )
    op.drop_constraint(
        "fk_patient_publication_supersedes",
        "patient_publications",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_patient_publication_version",
        "patient_publications",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_patient_publication_version",
        "patient_publications",
        "entry_versions",
        ["clinic_id", "entry_version_id"],
        ["clinic_id", "id"],
    )
    op.drop_constraint(
        "fk_patient_publication_entry",
        "patient_publications",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_patient_publication_scope_id",
        "patient_publications",
        type_="unique",
    )
    op.drop_column("patient_publications", "supersedes_publication_id")
    op.drop_column("patient_publications", "entry_id")

    op.drop_constraint(
        "uq_entry_version_clinic_entry_id", "entry_versions", type_="unique"
    )
    op.drop_constraint("uq_entry_clinic_patient_id", "entries", type_="unique")
