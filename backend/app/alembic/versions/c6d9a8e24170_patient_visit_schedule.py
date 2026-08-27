"""Add clinic-scoped patient visit scheduling.

Revision ID: c6d9a8e24170
Revises: b24f6d8e9130
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6d9a8e24170"
down_revision: str | None = "b24f6d8e9130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "patient_visits",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column(
            "clinic_id",
            postgresql.UUID(),
            sa.ForeignKey("clinics.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="scheduled"),
        sa.Column(
            "visit_type", sa.String(40), nullable=False, server_default="clinic_visit"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('scheduled','checked_in','in_progress','completed','cancelled','no_show')",
            name="ck_patient_visits_status",
        ),
        sa.UniqueConstraint("clinic_id", "id", name="uq_patient_visit_clinic_id"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_visit_patient",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_patient_visit_clinic_schedule",
        "patient_visits",
        ["clinic_id", "scheduled_at", "status"],
    )
    op.create_index(
        "ix_patient_visit_patient", "patient_visits", ["clinic_id", "patient_id"]
    )
    op.execute("ALTER TABLE patient_visits ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY clinic_isolation ON patient_visits
        USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON patient_visits TO nightingale_app"
    )


def downgrade() -> None:
    op.drop_table("patient_visits")
