"""add verified one-time clinic invitations

Revision ID: a13f4b6c9d20
Revises: f9127d3b4c50
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a13f4b6c9d20"
down_revision: str | None = "f9127d3b4c50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE clinic_invitations (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          email VARCHAR(255) NOT NULL,
          invited_full_name VARCHAR(255),
          role VARCHAR(20) NOT NULL,
          token_hash VARCHAR(64) NOT NULL UNIQUE,
          created_by_membership_id UUID NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          accepted_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT ck_clinic_invitation_role
            CHECK (role IN ('patient','staff','clinician','admin')),
          CONSTRAINT fk_clinic_invitation_creator_tenant
            FOREIGN KEY(clinic_id,created_by_membership_id)
            REFERENCES clinic_memberships(clinic_id,id)
        );
        CREATE INDEX ix_clinic_invitations_pending_email
          ON clinic_invitations(clinic_id,email,accepted_at);
        ALTER TABLE clinic_invitations ENABLE ROW LEVEL SECURITY;
        CREATE POLICY clinic_isolation ON clinic_invitations
          USING (
            clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
          )
          WITH CHECK (
            clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
          );
        GRANT SELECT, INSERT, UPDATE, DELETE ON clinic_invitations TO nightingale_app;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE clinic_invitations")
