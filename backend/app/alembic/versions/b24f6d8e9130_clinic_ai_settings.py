"""Add encrypted clinic-scoped AI processing settings.

Revision ID: b24f6d8e9130
Revises: a91c5e7d2042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b24f6d8e9130"
down_revision: str | None = "a91c5e7d2042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clinic_ai_settings",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column(
            "clinic_id",
            postgresql.UUID(),
            sa.ForeignKey("clinics.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(40), nullable=False, server_default="openai"),
        sa.Column("api_key_ciphertext", sa.LargeBinary()),
        sa.Column("api_key_last4", sa.String(4)),
        sa.Column(
            "fast_model", sa.String(160), nullable=False, server_default="gpt-5-mini"
        ),
        sa.Column(
            "careful_model", sa.String(160), nullable=False, server_default="gpt-5.1"
        ),
        sa.Column(
            "transcribe_model",
            sa.String(160),
            nullable=False,
            server_default="gpt-4o-transcribe-diarize",
        ),
        sa.Column("updated_by_membership_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("clinic_id", name="uq_clinic_ai_settings_clinic"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "updated_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_clinic_ai_settings_updater",
        ),
    )
    op.execute("ALTER TABLE clinic_ai_settings ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY clinic_isolation ON clinic_ai_settings
        USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON clinic_ai_settings TO nightingale_app"
    )


def downgrade() -> None:
    op.drop_table("clinic_ai_settings")
