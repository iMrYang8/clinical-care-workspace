"""Add delivery operations and external-retention attestations.

Revision ID: e4c9d65f2a13
Revises: e3b8c54d9e12
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4c9d65f2a13"
down_revision: str | None = "e3b8c54d9e12"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "notification_outbox",
        sa.Column("portal_invitation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_portal_invitation",
        "notification_outbox",
        "patient_portal_invitations",
        ["clinic_id", "portal_invitation_id"],
        ["clinic_id", "id"],
    )
    op.create_index(
        "ix_notification_portal_invitation",
        "notification_outbox",
        ["clinic_id", "portal_invitation_id"],
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_kind", sa.String(length=40), nullable=False),
        sa.Column("worker_version", sa.String(length=80), nullable=False),
        sa.Column(
            "source_commit",
            sa.String(length=64),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_kind", name="uq_worker_heartbeat_kind"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON worker_heartbeats TO nightingale_app")
    op.add_column(
        "patient_publications",
        sa.Column(
            "correction_idempotency_key_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "patient_publications",
        sa.Column("correction_request_sha256", sa.String(length=64), nullable=True),
    )
    # Historical correction rows predate addressable request hashes. Give each
    # one a unique legacy marker so it fails closed on a future retry rather
    # than inferring identity from encrypted outbox payloads. New writes store
    # the actual key and canonical-request SHA-256 values.
    bind = op.get_bind()
    legacy_corrections = bind.execute(
        sa.text(
            """
            SELECT DISTINCT replacement_publication_id
            FROM publication_correction_outreaches
            WHERE replacement_publication_id IS NOT NULL
            """
        )
    ).scalars()
    for publication_id in legacy_corrections:
        publication_text = str(publication_id)
        bind.execute(
            sa.text(
                """
                UPDATE patient_publications
                SET correction_idempotency_key_sha256 = :key_sha256,
                    correction_request_sha256 = :request_sha256
                WHERE id = :publication_id
                """
            ),
            {
                "publication_id": publication_id,
                "key_sha256": hashlib.sha256(
                    f"legacy-correction-key:{publication_text}".encode()
                ).hexdigest(),
                "request_sha256": hashlib.sha256(
                    f"legacy-correction-request:{publication_text}".encode()
                ).hexdigest(),
            },
        )
    op.create_check_constraint(
        "ck_patient_publication_correction_hashes",
        "patient_publications",
        "((correction_idempotency_key_sha256 IS NULL) = "
        "(correction_request_sha256 IS NULL)) AND "
        "(correction_idempotency_key_sha256 IS NULL OR "
        "(supersedes_publication_id IS NOT NULL AND "
        "correction_idempotency_key_sha256 ~ '^[0-9a-f]{64}$' AND "
        "correction_request_sha256 ~ '^[0-9a-f]{64}$'))",
    )
    op.create_unique_constraint(
        "uq_patient_publication_correction_idempotency",
        "patient_publications",
        ["clinic_id", "correction_idempotency_key_sha256"],
    )
    op.add_column(
        "patient_access_credentials",
        sa.Column(
            "preferred_channel",
            sa.String(length=20),
            nullable=False,
            server_default="sms",
        ),
    )
    op.create_check_constraint(
        "ck_patient_access_preferred_channel",
        "patient_access_credentials",
        "preferred_channel IN ('sms','whatsapp')",
    )
    op.alter_column(
        "patient_access_credentials", "preferred_channel", server_default=None
    )
    op.add_column(
        "clinic_operational_settings",
        sa.Column(
            "external_proxy_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "clinic_operational_settings",
        sa.Column(
            "external_container_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "clinic_operational_settings",
        sa.Column(
            "external_apm_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "clinic_operational_settings",
        sa.Column(
            "external_observability_retention_evidence",
            sa.String(length=40),
            nullable=False,
            server_default="unqualified",
        ),
    )
    op.add_column(
        "clinic_operational_settings",
        sa.Column(
            "external_observability_retention_evidence_id",
            sa.String(length=200),
            nullable=False,
            server_default="migration:e4:legacy-unqualified",
        ),
    )
    op.create_check_constraint(
        "ck_clinic_external_proxy_retention",
        "clinic_operational_settings",
        "external_proxy_retention_days BETWEEN 1 AND 30",
    )
    op.create_check_constraint(
        "ck_clinic_external_container_retention",
        "clinic_operational_settings",
        "external_container_retention_days BETWEEN 1 AND 30",
    )
    op.create_check_constraint(
        "ck_clinic_external_apm_retention",
        "clinic_operational_settings",
        "external_apm_retention_days BETWEEN 1 AND 30",
    )
    op.create_check_constraint(
        "ck_clinic_external_retention_evidence",
        "clinic_operational_settings",
        "external_observability_retention_evidence IN "
        "('unqualified','deterministic_fixture','deployment_policy','provider_contract')",
    )
    for column in (
        "external_proxy_retention_days",
        "external_container_retention_days",
        "external_apm_retention_days",
        "external_observability_retention_evidence",
        "external_observability_retention_evidence_id",
    ):
        op.alter_column("clinic_operational_settings", column, server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_clinic_external_retention_evidence",
        "clinic_operational_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_clinic_external_apm_retention",
        "clinic_operational_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_clinic_external_container_retention",
        "clinic_operational_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_clinic_external_proxy_retention",
        "clinic_operational_settings",
        type_="check",
    )
    op.drop_column(
        "clinic_operational_settings",
        "external_observability_retention_evidence_id",
    )
    op.drop_column(
        "clinic_operational_settings", "external_observability_retention_evidence"
    )
    op.drop_column("clinic_operational_settings", "external_apm_retention_days")
    op.drop_column("clinic_operational_settings", "external_container_retention_days")
    op.drop_column("clinic_operational_settings", "external_proxy_retention_days")
    op.drop_constraint(
        "ck_patient_access_preferred_channel",
        "patient_access_credentials",
        type_="check",
    )
    op.drop_column("patient_access_credentials", "preferred_channel")
    op.drop_constraint(
        "uq_patient_publication_correction_idempotency",
        "patient_publications",
        type_="unique",
    )
    op.drop_constraint(
        "ck_patient_publication_correction_hashes",
        "patient_publications",
        type_="check",
    )
    op.drop_column("patient_publications", "correction_request_sha256")
    op.drop_column("patient_publications", "correction_idempotency_key_sha256")
    op.drop_index(
        "ix_notification_portal_invitation", table_name="notification_outbox"
    )
    op.drop_constraint(
        "fk_notification_portal_invitation",
        "notification_outbox",
        type_="foreignkey",
    )
    op.drop_column("notification_outbox", "portal_invitation_id")
    op.execute("REVOKE ALL ON worker_heartbeats FROM nightingale_app")
    op.drop_table("worker_heartbeats")
