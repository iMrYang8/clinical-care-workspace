"""Bind worker-created entries to a trusted job claim.

Revision ID: a91e6c243b40
Revises: c7b13d0a9e21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a91e6c243b40"
down_revision: str | None = "c7b13d0a9e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("entries", sa.Column("source_job_id", sa.Uuid(), nullable=True))
    op.create_index("ix_entries_source_job_id", "entries", ["source_job_id"])


def downgrade() -> None:
    op.drop_index("ix_entries_source_job_id", table_name="entries")
    op.drop_column("entries", "source_job_id")
