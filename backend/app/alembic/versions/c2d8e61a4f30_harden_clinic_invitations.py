"""harden clinic invitation lifecycle

Revision ID: c2d8e61a4f30
Revises: a13f4b6c9d20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c2d8e61a4f30"
down_revision: str | None = "a13f4b6c9d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE clinic_invitations
          ADD COLUMN revoked_at TIMESTAMPTZ;
        ALTER TABLE clinic_invitations
          DROP CONSTRAINT ck_clinic_invitation_role;
        ALTER TABLE clinic_invitations
          ADD CONSTRAINT ck_clinic_invitation_role
          CHECK (role IN ('staff','clinician','admin'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE clinic_invitations
          DROP CONSTRAINT ck_clinic_invitation_role;
        ALTER TABLE clinic_invitations
          ADD CONSTRAINT ck_clinic_invitation_role
          CHECK (role IN ('patient','staff','clinician','admin'));
        ALTER TABLE clinic_invitations DROP COLUMN revoked_at;
        """
    )
