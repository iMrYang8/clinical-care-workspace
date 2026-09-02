"""Guard medication ranking and isolate platform oversight tables.

Revision ID: e7a3c8d51b62
Revises: e6f2b8c0d315
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7a3c8d51b62"
down_revision: str | None = "e6f2b8c0d315"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "nightingale_app"

# Platform oversight tables are cross-clinic by design, so they carry no
# clinic_id and are therefore skipped by the clinic-scoped RLS installer. They
# are still writable by the runtime role, which left them as the only
# runtime-reachable tables with no row-level policy at all.
PLATFORM_TABLES = ("platform_administrators", "platform_audit_events")


def upgrade() -> None:
    # A learned negative medication adjustment may already exist. Repair it
    # before installing the fail-closed database guard, mirroring the allergy
    # floor installed in e6f2b8c0d315.
    op.execute(
        "UPDATE highlights SET learned_score = GREATEST(learned_score, 0) "
        "WHERE feature_keys_json @> '[\"entity:medication\"]'::jsonb"
    )
    op.create_check_constraint(
        "ck_highlight_medication_learning_floor",
        "highlights",
        "NOT (feature_keys_json @> '[\"entity:medication\"]'::jsonb) "
        "OR learned_score >= 0",
    )

    # Clinic staff sessions must never read or write the platform oversight
    # tables. Authentication reads them through a SECURITY DEFINER lookup and
    # the platform routes bind the platform_admin actor role before touching
    # them, so a single actor-role policy is sufficient and fails closed when
    # the GUC is unset.
    for table in PLATFORM_TABLES:
        op.execute(
            f"""
            ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;
            ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;
            CREATE POLICY platform_actor_scope ON "{table}"
            USING (
              current_setting('app.current_actor_role', true) = 'platform_admin'
            )
            WITH CHECK (
              current_setting('app.current_actor_role', true) = 'platform_admin'
            );
            GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO {RUNTIME_ROLE};
            """
        )

    # e5d1f7a9c204 backfilled source_role with origin taking precedence over
    # section, which recorded a patient's own statement captured by the AI
    # pipeline as "system" and erased patient-versus-staff attribution on
    # conflict cards. The capture path remains available in origin.
    op.execute(
        """
        UPDATE clinical_fact_assertions
        SET source_role = 'patient'
        WHERE source_section = 'patient'
          AND origin IN ('ai', 'system')
          AND source_role = 'system'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE clinical_fact_assertions
        SET source_role = 'system'
        WHERE source_section = 'patient'
          AND origin IN ('ai', 'system')
          AND source_role = 'patient'
        """
    )
    for table in PLATFORM_TABLES:
        op.execute(
            f"""
            DROP POLICY IF EXISTS platform_actor_scope ON "{table}";
            ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;
            ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;
            """
        )
    op.drop_constraint(
        "ck_highlight_medication_learning_floor", "highlights", type_="check"
    )
