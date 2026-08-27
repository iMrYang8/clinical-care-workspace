"""Add patient registry, platform administration, and trust decisions.

Revision ID: f3a8c71d2e40
Revises: e4b7c91a2d30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3a8c71d2e40"
down_revision: str | None = "e4b7c91a2d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "patient_identifiers",
    "patient_portal_invitations",
    "importance_impressions",
    "decision_assessments",
    "patient_publications",
)


def _tenant_policy(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        f"""
        CREATE POLICY clinic_isolation ON "{table}"
        USING (
          clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
        )
        WITH CHECK (
          clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
        )
        """
    )


def upgrade() -> None:
    op.add_column("patients", sa.Column("date_of_birth_ciphertext", sa.LargeBinary()))
    op.add_column("patients", sa.Column("identity_match_hash", sa.String(64)))
    op.add_column(
        "patients", sa.Column("status", sa.String(20), nullable=False, server_default="active")
    )
    op.add_column("patients", sa.Column("created_by_membership_id", postgresql.UUID()))
    op.add_column(
        "patients",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_patients_identity_match_hash", "patients", ["identity_match_hash"])
    op.create_foreign_key(
        "fk_patient_creator_membership",
        "patients",
        "clinic_memberships",
        ["clinic_id", "created_by_membership_id"],
        ["clinic_id", "id"],
    )

    op.create_table(
        "patient_identifiers",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(), nullable=False),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("identifier_type", sa.String(40), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("value_hmac", sa.String(64), nullable=False),
        sa.Column("masked_suffix", sa.String(8), nullable=False),
        sa.Column("created_by_membership_id", postgresql.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint(
            "clinic_id", "identifier_type", "value_hmac", name="uq_patient_identifier"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinics.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_identifier_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_identifier_creator",
        ),
    )
    op.create_index(
        "ix_patient_identifier_patient", "patient_identifiers", ["clinic_id", "patient_id"]
    )
    op.create_table(
        "patient_portal_invitations",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(), nullable=False),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_by_membership_id", postgresql.UUID(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinics.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_portal_invitation_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "created_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_portal_invitation_creator",
        ),
        sa.UniqueConstraint(
            "token_hash", name="patient_portal_invitation_token_key"
        ),
    )
    op.create_index(
        "ix_patient_portal_invitation_pending",
        "patient_portal_invitations",
        ["clinic_id", "patient_id", "accepted_at"],
    )

    op.create_table(
        "platform_administrators",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="platform_admin_user_key"),
    )
    op.create_table(
        "platform_audit_events",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("platform_admin_id", postgresql.UUID(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_clinic_id", postgresql.UUID()),
        sa.Column("target_patient_id", postgresql.UUID()),
        sa.Column("request_id", sa.String(100), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["platform_admin_id"], ["platform_administrators.id"]),
        sa.ForeignKeyConstraint(["target_clinic_id"], ["clinics.id"]),
        sa.ForeignKeyConstraint(
            ["target_clinic_id", "target_patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_platform_audit_patient",
        ),
    )
    op.create_index("ix_platform_audit_created", "platform_audit_events", ["created_at"])

    op.create_table(
        "importance_impressions",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(), nullable=False),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("highlight_id", postgresql.UUID(), nullable=False),
        sa.Column("viewer_membership_id", postgresql.UUID(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("surface", sa.String(60), nullable=False, server_default="current_priorities"),
        sa.Column(
            "shown_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint("rank BETWEEN 1 AND 5", name="ck_importance_impression_rank"),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinics.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_importance_impression_highlight",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "viewer_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_importance_impression_viewer",
        ),
    )
    op.create_index(
        "ix_importance_impression_shown", "importance_impressions", ["clinic_id", "shown_at"]
    )
    op.create_table(
        "decision_assessments",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(), nullable=False),
        sa.Column("highlight_id", postgresql.UUID(), nullable=False),
        sa.Column("output_type", sa.String(40), nullable=False, server_default="extracted_fact"),
        sa.Column("support_state", sa.String(30), nullable=False, server_default="supported"),
        sa.Column("risk_tier", sa.String(20), nullable=False, server_default="standard"),
        sa.Column("risk_rule_version", sa.String(60), nullable=False, server_default="risk-rules-v1"),
        sa.Column("risk_rule_ids_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("confidence_value", sa.Float()),
        sa.Column("confidence_band", sa.String(20), nullable=False, server_default="unavailable"),
        sa.Column("calibration_version", sa.String(80)),
        sa.Column("abstained", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("abstention_reason", sa.String(80)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("clinic_id", "highlight_id", name="uq_decision_assessment_highlight"),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinics.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "highlight_id"],
            ["highlights.clinic_id", "highlights.id"],
            name="fk_decision_assessment_highlight",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "patient_publications",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column("clinic_id", postgresql.UUID(), nullable=False),
        sa.Column("patient_id", postgresql.UUID(), nullable=False),
        sa.Column("entry_version_id", postgresql.UUID(), nullable=False),
        sa.Column("approved_by_membership_id", postgresql.UUID(), nullable=False),
        sa.Column("approval_policy_version", sa.String(80), nullable=False, server_default="patient-sharing-v1"),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["clinic_id"], ["clinics.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "patient_id"],
            ["patients.clinic_id", "patients.id"],
            name="fk_patient_publication_patient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "entry_version_id"],
            ["entry_versions.clinic_id", "entry_versions.id"],
            name="fk_patient_publication_version",
        ),
        sa.ForeignKeyConstraint(
            ["clinic_id", "approved_by_membership_id"],
            ["clinic_memberships.clinic_id", "clinic_memberships.id"],
            name="fk_patient_publication_approver",
        ),
    )
    op.create_index(
        "ix_patient_publication_active",
        "patient_publications",
        ["clinic_id", "patient_id", "withdrawn_at"],
    )

    for name, type_ in (
        ("fact_type", sa.String(40)),
        ("normalized_key", sa.String(200)),
        ("left_version_id", postgresql.UUID()),
        ("right_version_id", postgresql.UUID()),
        ("left_pointer_id", postgresql.UUID()),
        ("right_pointer_id", postgresql.UUID()),
        ("severity", sa.String(20)),
        ("resolution", sa.String(500)),
        ("resolved_by_membership_id", postgresql.UUID()),
        ("resolved_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("conflict_cases", sa.Column(name, type_, nullable=True))
    op.execute("UPDATE conflict_cases SET fact_type='clinical', normalized_key='', severity='high'")
    op.alter_column("conflict_cases", "fact_type", nullable=False, server_default="clinical")
    op.alter_column("conflict_cases", "normalized_key", nullable=False, server_default="")
    op.alter_column("conflict_cases", "severity", nullable=False, server_default="high")
    for name, local_column, remote_table, remote_column in (
        ("fk_conflict_left_version", "left_version_id", "entry_versions", "id"),
        ("fk_conflict_right_version", "right_version_id", "entry_versions", "id"),
        ("fk_conflict_left_pointer", "left_pointer_id", "provenance_pointers", "id"),
        ("fk_conflict_right_pointer", "right_pointer_id", "provenance_pointers", "id"),
        (
            "fk_conflict_resolver",
            "resolved_by_membership_id",
            "clinic_memberships",
            "id",
        ),
    ):
        op.create_foreign_key(
            name,
            "conflict_cases",
            remote_table,
            ["clinic_id", local_column],
            ["clinic_id", remote_column],
        )
    op.create_index(
        "ix_conflict_patient_status",
        "conflict_cases",
        ["clinic_id", "patient_id", "status"],
    )

    for table in TENANT_TABLES:
        _tenant_policy(table)
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        + ", ".join((*TENANT_TABLES, "platform_administrators", "platform_audit_events"))
        + " TO nightingale_app"
    )


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    op.drop_index("ix_platform_audit_created", table_name="platform_audit_events")
    op.drop_table("platform_audit_events")
    op.drop_table("platform_administrators")
    op.drop_index("ix_conflict_patient_status", table_name="conflict_cases")
    for column in (
        "resolved_at",
        "resolved_by_membership_id",
        "resolution",
        "severity",
        "right_pointer_id",
        "left_pointer_id",
        "right_version_id",
        "left_version_id",
        "normalized_key",
        "fact_type",
    ):
        op.drop_column("conflict_cases", column)
    op.drop_constraint("fk_patient_creator_membership", "patients", type_="foreignkey")
    op.drop_index("ix_patients_identity_match_hash", table_name="patients")
    for column in (
        "updated_at",
        "created_by_membership_id",
        "status",
        "identity_match_hash",
        "date_of_birth_ciphertext",
    ):
        op.drop_column("patients", column)
