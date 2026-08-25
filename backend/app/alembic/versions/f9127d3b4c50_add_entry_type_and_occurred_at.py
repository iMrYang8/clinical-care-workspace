"""add formal entry type and clinical occurrence time

Revision ID: f9127d3b4c50
Revises: ee8a2f6b9010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9127d3b4c50"
down_revision: str | None = "ee8a2f6b9010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entries",
        sa.Column(
            "entry_type",
            sa.String(length=60),
            nullable=False,
            server_default="system_record",
        ),
    )
    op.add_column(
        "entries",
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_entries_entry_type", "entries", ["entry_type"])
    op.create_index("ix_entries_occurred_at", "entries", ["occurred_at"])
    op.alter_column("entries", "entry_type", server_default=None)
    op.alter_column("entries", "occurred_at", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_entries_occurred_at", table_name="entries")
    op.drop_index("ix_entries_entry_type", table_name="entries")
    op.drop_column("entries", "occurred_at")
    op.drop_column("entries", "entry_type")
