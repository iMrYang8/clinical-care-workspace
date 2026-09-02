"""Let a patient's own note create provenance without a self-referential check.

Revision ID: e9a7c3b18d24
Revises: e8c1d4a92b70
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e9a7c3b18d24"
down_revision: str | None = "e8c1d4a92b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reading a pointer stays gated on the pointer's own linkage. That is the
# correct and narrower test once the row exists.
READ_EXPRESSION = "app_pointer_context_allows(clinic_id, id)"

# Writing one cannot use that test. WITH CHECK is evaluated before the new row
# is visible, so a self-referential EXISTS over provenance_pointers is always
# false for a patient actor: every patient-authored note whose text produced a
# clinical fact failed with a 500. Non-patient actors never noticed, because
# app_nonpatient_context_allows short-circuits the whole expression first.
#
# A pointer always carries its immutable source version, so the write test is
# the parent it is being attached to. That is the same fail-closed patient fence
# every other child table already uses.
WRITE_EXPRESSION = "app_version_context_allows(clinic_id, entry_version_id)"


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS patient_scope ON provenance_pointers")
    op.execute(
        f"""
        CREATE POLICY patient_scope ON provenance_pointers AS RESTRICTIVE
        USING ({READ_EXPRESSION})
        WITH CHECK ({WRITE_EXPRESSION})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS patient_scope ON provenance_pointers")
    op.execute(
        f"""
        CREATE POLICY patient_scope ON provenance_pointers AS RESTRICTIVE
        USING ({READ_EXPRESSION})
        WITH CHECK ({READ_EXPRESSION})
        """
    )
