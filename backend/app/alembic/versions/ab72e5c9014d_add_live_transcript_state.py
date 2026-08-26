"""add auditable live transcript state

Revision ID: ab72e5c9014d
Revises: c2d8e61a4f30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab72e5c9014d"
down_revision: str | None = "c2d8e61a4f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "voice_sessions",
        sa.Column(
            "live_transcript_status",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'not_started'"),
        ),
    )
    op.add_column(
        "voice_sessions",
        sa.Column("live_transcript_error_code", sa.String(length=80), nullable=True),
    )
    op.create_check_constraint(
        "ck_voice_session_live_transcript_status",
        "voice_sessions",
        "live_transcript_status IN "
        "('not_started','available','unavailable','needs_review','replaced')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_voice_session_live_transcript_status",
        "voice_sessions",
        type_="check",
    )
    op.drop_column("voice_sessions", "live_transcript_error_code")
    op.drop_column("voice_sessions", "live_transcript_status")
