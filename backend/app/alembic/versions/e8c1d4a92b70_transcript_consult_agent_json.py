"""Store proposal-only consult-agent payload on transcript revisions.

Revision ID: e8c1d4a92b70
Revises: e7a3c8d51b62
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8c1d4a92b70"
down_revision: str | None = "e7a3c8d51b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transcript_revisions",
        sa.Column(
            "consult_agent_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("transcript_revisions", "consult_agent_json", server_default=None)


def downgrade() -> None:
    op.drop_column("transcript_revisions", "consult_agent_json")
