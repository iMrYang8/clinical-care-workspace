"""add human-readable clinic code

Revision ID: e4b7c91a2d30
Revises: ab72e5c9014d
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b7c91a2d30"
down_revision: str | None = "ab72e5c9014d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LETTERS_ONLY = re.compile(r"[^A-Z]")


def _fallback_code(clinic_id: object, attempt: int) -> str:
    """Produce a deterministic, letters-only code without exposing the UUID."""

    digest = hashlib.sha256(f"{clinic_id}:{attempt}".encode()).digest()
    return "C" + "".join(chr(ord("A") + byte % 26) for byte in digest[:11])


def upgrade() -> None:
    op.add_column(
        "clinics",
        sa.Column("code", sa.String(length=12), nullable=True),
    )

    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text("SELECT id, slug FROM clinics ORDER BY id")
        ).mappings()
    )
    assigned: dict[object, str] = {}
    used: set[str] = set()

    # Preserve the two stable local fixture identities expected by the UI and
    # tests. The remaining rows are migrated without relying on names or UUIDs
    # being exposed as the operator-facing login identifier.
    fixture_codes = {
        "nightingale-demo": "NIGHTINGALE",
        "other-demo": "OTHERCLINIC",
    }
    for row in rows:
        code = fixture_codes.get(str(row["slug"]))
        if code is not None:
            assigned[row["id"]] = code
            used.add(code)

    for row in rows:
        if row["id"] in assigned:
            continue
        candidate = _LETTERS_ONLY.sub("", str(row["slug"]).upper())[:12]
        if len(candidate) < 3 or candidate in used:
            attempt = 0
            candidate = _fallback_code(row["id"], attempt)
            while candidate in used:
                attempt += 1
                candidate = _fallback_code(row["id"], attempt)
        assigned[row["id"]] = candidate
        used.add(candidate)

    for clinic_id, code in assigned.items():
        connection.execute(
            sa.text("UPDATE clinics SET code = :code WHERE id = :clinic_id"),
            {"clinic_id": clinic_id, "code": code},
        )

    op.alter_column("clinics", "code", existing_type=sa.String(12), nullable=False)
    op.create_check_constraint(
        "ck_clinics_code_format",
        "clinics",
        "code ~ '^[A-Z]{3,12}$'",
    )
    op.create_unique_constraint("clinics_code_key", "clinics", ["code"])
    op.create_index("ix_clinics_code", "clinics", ["code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_clinics_code", table_name="clinics")
    op.drop_constraint("clinics_code_key", "clinics", type_="unique")
    op.drop_constraint("ck_clinics_code_format", "clinics", type_="check")
    op.drop_column("clinics", "code")
