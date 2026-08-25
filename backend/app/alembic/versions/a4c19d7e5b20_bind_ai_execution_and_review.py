"""Bind AI execution to a worker and persist encrypted two-stage review.

Revision ID: a4c19d7e5b20
Revises: f6a2d91c4e80
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c19d7e5b20"
down_revision: str | None = "f6a2d91c4e80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_attempts",
        sa.Column("worker_membership_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_job_attempt_worker_tenant",
        "job_attempts",
        "clinic_memberships",
        ["clinic_id", "worker_membership_id"],
        ["clinic_id", "id"],
    )
    op.create_index(
        "ix_job_attempt_worker",
        "job_attempts",
        ["clinic_id", "worker_membership_id"],
    )

    op.add_column(
        "ai_runs",
        sa.Column("executed_by_worker_membership_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "ai_runs", sa.Column("review_model", sa.String(length=160), nullable=True)
    )
    op.add_column(
        "ai_runs",
        sa.Column(
            "review_status",
            sa.String(length=30),
            nullable=False,
            server_default="not_required",
        ),
    )
    op.add_column(
        "ai_runs", sa.Column("primary_output_ciphertext", sa.LargeBinary(), nullable=True)
    )
    op.add_column(
        "ai_runs", sa.Column("review_output_ciphertext", sa.LargeBinary(), nullable=True)
    )
    op.create_foreign_key(
        "fk_ai_run_worker_tenant",
        "ai_runs",
        "clinic_memberships",
        ["clinic_id", "executed_by_worker_membership_id"],
        ["clinic_id", "id"],
    )
    op.create_index(
        "ix_ai_run_worker",
        "ai_runs",
        ["clinic_id", "executed_by_worker_membership_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_run_worker", table_name="ai_runs")
    op.drop_constraint("fk_ai_run_worker_tenant", "ai_runs", type_="foreignkey")
    op.drop_column("ai_runs", "review_output_ciphertext")
    op.drop_column("ai_runs", "primary_output_ciphertext")
    op.drop_column("ai_runs", "review_status")
    op.drop_column("ai_runs", "review_model")
    op.drop_column("ai_runs", "executed_by_worker_membership_id")

    op.drop_index("ix_job_attempt_worker", table_name="job_attempts")
    op.drop_constraint(
        "fk_job_attempt_worker_tenant", "job_attempts", type_="foreignkey"
    )
    op.drop_column("job_attempts", "worker_membership_id")
