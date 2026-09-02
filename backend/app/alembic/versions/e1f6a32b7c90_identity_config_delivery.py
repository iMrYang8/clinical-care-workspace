"""Add phone patient identity, clinic configuration, and delivery records.

Revision ID: e1f6a32b7c90
Revises: d4e8a91c7b62
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f6a32b7c90"
down_revision: str | None = "d4e8a91c7b62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_id() -> sa.Column:
    return sa.Column(
        "clinic_id",
        postgresql.UUID(),
        sa.ForeignKey("clinics.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    # Existing rows remain staff identities unless their only relationship is a
    # patient portal link.  Email/password flows continue unchanged for staff.
    op.add_column(
        "users",
        sa.Column(
            "account_kind", sa.String(20), nullable=False, server_default="staff"
        ),
    )
    op.execute(
        """
        UPDATE users AS identity
        SET account_kind = 'patient'
        WHERE EXISTS (
          SELECT 1 FROM patient_user_links AS link WHERE link.user_id = identity.id
        )
          AND NOT EXISTS (
            SELECT 1 FROM clinic_memberships AS membership
            WHERE membership.user_id = identity.id
              AND membership.is_active
              AND membership.role <> 'patient'
          )
        """
    )
    op.execute(
        """
        UPDATE users AS identity
        SET account_kind = 'service'
        WHERE account_kind = 'staff'
          AND EXISTS (
            SELECT 1 FROM clinic_memberships AS membership
            WHERE membership.user_id = identity.id
              AND membership.is_active
              AND membership.role = 'worker'
          )
          AND NOT EXISTS (
            SELECT 1 FROM clinic_memberships AS membership
            WHERE membership.user_id = identity.id
              AND membership.is_active
              AND membership.role <> 'worker'
          )
        """
    )
    op.alter_column("users", "email", existing_type=sa.String(255), nullable=True)
    op.alter_column("users", "hashed_password", existing_type=sa.String(), nullable=True)
    op.create_check_constraint(
        "ck_users_account_kind",
        "users",
        "account_kind IN ('staff','patient','service')",
    )
    op.create_check_constraint(
        "ck_users_staff_credentials",
        "users",
        "account_kind = 'patient' OR "
        "(email IS NOT NULL AND hashed_password IS NOT NULL)",
    )
    op.create_index("ix_users_account_kind", "users", ["account_kind"])
    op.alter_column(
        "users", "account_kind", existing_type=sa.String(20), server_default=None
    )

    # The original email invitation remains a supported delivery channel.  A
    # phone-only invitation stores NULL rather than a fabricated email address.
    op.alter_column(
        "patient_portal_invitations",
        "email",
        existing_type=sa.String(255),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_patient_portal_invitation_clinic_id",
        "patient_portal_invitations",
        ["clinic_id", "id"],
    )

    op.add_column(
        "clinic_invitations",
        sa.Column("created_by_platform_admin_id", postgresql.UUID()),
    )
    op.alter_column(
        "clinic_invitations",
        "created_by_membership_id",
        existing_type=postgresql.UUID(),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_clinic_invitation_platform_creator",
        "clinic_invitations",
        "platform_administrators",
        ["created_by_platform_admin_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_clinic_invitation_exactly_one_creator",
        "clinic_invitations",
        "(created_by_membership_id IS NOT NULL)::int + "
        "(created_by_platform_admin_id IS NOT NULL)::int = 1",
    )

    op.create_table(
        "patient_access_credentials",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("user_id", postgresql.UUID()),
        sa.Column("invitation_id", postgresql.UUID()),
        sa.Column("portal_id", sa.String(80), nullable=False),
        sa.Column("phone_ciphertext", sa.LargeBinary()),
        sa.Column("phone_hmac", sa.String(64)),
        sa.Column("masked_phone", sa.String(32)),
        sa.Column("claim_code_hash", sa.String(64), nullable=False),
        sa.Column("claim_code_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_code_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_membership_id", postgresql.UUID()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("recovery_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "clinic_id", "id", name="uq_patient_access_credential_id"
        ),
        sa.UniqueConstraint("portal_id", name="patient_access_portal_id_key"),
        sa.UniqueConstraint(
            "claim_code_hash", name="patient_access_claim_code_key"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_access_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "invitation_id"],
            [
                "patient_portal_invitations.clinic_id",
                "patient_portal_invitations.id",
            ],
            name="fk_patient_access_invitation",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_access_creator",
        ),
    )
    op.create_index(
        "ix_patient_access_phone_hmac",
        "patient_access_credentials",
        ["clinic_id", "phone_hmac"],
    )
    op.create_index(
        "uq_patient_access_active_patient",
        "patient_access_credentials",
        ["clinic_id", "patient_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND revoked_at IS NULL"),
    )

    op.create_table(
        "patient_otp_challenges",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("credential_id", postgresql.UUID(), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("challenge_token_hash", sa.String(64), nullable=False),
        sa.Column("otp_hash", sa.String(255), nullable=False),
        sa.Column("attempts_remaining", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("resend_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purpose IN ('enrollment','login','recovery','phone_change')",
            name="ck_patient_otp_purpose",
        ),
        sa.UniqueConstraint(
            "challenge_token_hash", name="patient_otp_token_hash_key"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "credential_id"],
            ["patient_access_credentials.clinic_id", "patient_access_credentials.id"],
            name="fk_patient_otp_credential",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_patient_otp_active",
        "patient_otp_challenges",
        ["clinic_id", "credential_id", "expires_at"],
    )

    op.create_table(
        "clinic_operational_settings",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column(
            "timezone", sa.String(80), nullable=False, server_default="Asia/Singapore"
        ),
        sa.Column("worker_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "supported_languages_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"en\",\"ms\",\"nan\",\"zh\"]'::jsonb"),
        ),
        sa.Column(
            "messaging_channels_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "remote_text_egress_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "remote_audio_egress_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "calibration_required", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "onboarding_status", sa.String(20), nullable=False, server_default="draft"
        ),
        sa.Column("updated_by_platform_admin_id", postgresql.UUID()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("clinic_id", name="uq_clinic_operational_setting"),
        sa.CheckConstraint(
            "onboarding_status IN ('draft','ready','blocked')",
            name="ck_clinic_operational_onboarding",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_platform_admin_id"], ["platform_administrators.id"]
        ),
    )
    op.execute(
        """
        INSERT INTO clinic_operational_settings (
          id, clinic_id, timezone, worker_enabled, supported_languages_json,
          messaging_channels_json, remote_text_egress_enabled,
          remote_audio_egress_enabled, calibration_required,
          onboarding_status, updated_at
        )
        SELECT clinic.id,
               clinic.id,
               'Asia/Singapore',
               EXISTS (
                 SELECT 1 FROM clinic_memberships AS membership
                 WHERE membership.clinic_id = clinic.id
                   AND membership.is_active
                   AND membership.role = 'worker'
               ),
               '["en","ms","nan","zh"]'::jsonb,
               '[]'::jsonb,
               false,
               false,
               true,
               'draft',
               now()
        FROM clinics AS clinic
        ON CONFLICT (clinic_id) DO NOTHING
        """
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID()),
        sa.Column("visit_id", postgresql.UUID()),
        sa.Column("publication_id", postgresql.UUID()),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("destination_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("destination_masked", sa.String(120), nullable=False),
        sa.Column("template_key", sa.String(80), nullable=False),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("created_by_membership_id", postgresql.UUID()),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('queued','submitted','delivered','failed','acknowledged','revoked')",
            name="ck_notification_outbox_state",
        ),
        sa.CheckConstraint(
            "channel IN ('email','sms','whatsapp','portal')",
            name="ck_notification_outbox_channel",
        ),
        sa.UniqueConstraint("clinic_id", "id", name="uq_notification_outbox_id"),
        sa.UniqueConstraint(
            "clinic_id", "idempotency_key", name="uq_notification_idempotency"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_notification_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "visit_id"],
            ["patient_visits.clinic_id", "patient_visits.id"],
            name="fk_notification_visit",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_notification_publication",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_notification_creator",
        ),
    )
    op.create_index(
        "ix_notification_dispatch",
        "notification_outbox",
        ["clinic_id", "state", "available_at"],
    )
    op.create_index(
        "ix_notification_patient",
        "notification_outbox",
        ["clinic_id", "patient_id", "created_at"],
    )

    op.create_table(
        "notification_attempts",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("notification_id", postgresql.UUID(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("provider_message_id", sa.String(200)),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="started"),
        sa.Column("error_class", sa.String(80)),
        sa.Column("error_code", sa.String(80)),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "clinic_id", "notification_id", "attempt_no", name="uq_notification_attempt"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_notification_attempt_outbox",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_notification_attempt_message",
        "notification_attempts",
        ["clinic_id", "provider", "provider_message_id"],
    )

    op.create_table(
        "notification_receipts",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("notification_id", postgresql.UUID(), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("provider_event_id", sa.String(200), nullable=False),
        sa.Column("provider_message_id", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column(
            "signature_verified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "provider",
            "provider_event_id",
            name="uq_notification_receipt_event",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_notification_receipt_outbox",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_notification_receipt_outbox",
        "notification_receipts",
        ["clinic_id", "notification_id"],
    )

    op.create_table(
        "patient_publication_acknowledgements",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("publication_id", postgresql.UUID(), nullable=False),
        sa.Column("notification_id", postgresql.UUID()),
        sa.Column("acknowledged_by_user_id", postgresql.UUID(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="portal"),
        sa.Column(
            "event_type", sa.String(40), nullable=False, server_default="acknowledged"
        ),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "publication_id",
            "acknowledged_by_user_id",
            "event_type",
            name="uq_patient_publication_ack",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_publication_ack_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_publication_ack_publication",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_publication_ack_notification",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_user_id"], ["users.id"]
        ),
    )

    op.create_table(
        "publication_correction_outreaches",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("withdrawn_publication_id", postgresql.UUID(), nullable=False),
        sa.Column("replacement_publication_id", postgresql.UUID()),
        sa.Column("notification_id", postgresql.UUID()),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "withdrawn_publication_id",
            "replacement_publication_id",
            "notification_id",
            name="uq_correction_outreach_delivery",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_correction_outreach_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "withdrawn_publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_correction_outreach_withdrawn",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "replacement_publication_id"],
            ["patient_publications.clinic_id", "patient_publications.id"],
            name="fk_correction_outreach_replacement",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "notification_id"],
            ["notification_outbox.clinic_id", "notification_outbox.id"],
            name="fk_correction_outreach_notification",
        ),
    )
    op.create_index(
        "ix_correction_outreach_status",
        "publication_correction_outreaches",
        ["clinic_id", "status", "due_at"],
    )

    op.create_table(
        "patient_portal_events",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        _tenant_id(),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("aggregate_type", sa.String(40), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(), nullable=False),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_portal_event_patient",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_patient_portal_event_cursor",
        "patient_portal_events",
        ["clinic_id", "patient_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("patient_portal_events")
    op.drop_table("publication_correction_outreaches")
    op.drop_table("patient_publication_acknowledgements")
    op.drop_table("notification_receipts")
    op.drop_table("notification_attempts")
    op.drop_table("notification_outbox")
    op.drop_table("clinic_operational_settings")
    op.drop_table("patient_otp_challenges")
    op.drop_table("patient_access_credentials")

    op.drop_constraint(
        "ck_clinic_invitation_exactly_one_creator",
        "clinic_invitations",
        type_="check",
    )
    op.drop_constraint(
        "fk_clinic_invitation_platform_creator",
        "clinic_invitations",
        type_="foreignkey",
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM clinic_invitations
            WHERE created_by_membership_id IS NULL
          ) THEN
            RAISE EXCEPTION
              'platform-created clinic invitations must be migrated before downgrade';
          END IF;
        END;
        $$;
        """
    )
    op.alter_column(
        "clinic_invitations",
        "created_by_membership_id",
        existing_type=postgresql.UUID(),
        nullable=False,
    )
    op.drop_column("clinic_invitations", "created_by_platform_admin_id")

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM patient_portal_invitations WHERE email IS NULL
          ) THEN
            RAISE EXCEPTION
              'phone-only portal invitations must be migrated before downgrade';
          END IF;
        END;
        $$;
        """
    )
    op.alter_column(
        "patient_portal_invitations",
        "email",
        existing_type=sa.String(255),
        nullable=False,
    )
    op.drop_constraint(
        "uq_patient_portal_invitation_clinic_id",
        "patient_portal_invitations",
        type_="unique",
    )
    op.drop_index("ix_users_account_kind", table_name="users")
    op.drop_constraint("ck_users_staff_credentials", "users", type_="check")
    op.drop_constraint("ck_users_account_kind", "users", type_="check")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM users
            WHERE email IS NULL OR hashed_password IS NULL
          ) THEN
            RAISE EXCEPTION
              'phone-only patient identities must be migrated before downgrade';
          END IF;
        END;
        $$;
        """
    )
    op.alter_column("users", "hashed_password", existing_type=sa.String(), nullable=False)
    op.alter_column("users", "email", existing_type=sa.String(255), nullable=False)
    op.drop_column("users", "account_kind")
