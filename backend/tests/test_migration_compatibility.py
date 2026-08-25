from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlmodel import Session

from app.seed import demo_id


def test_upgraded_entries_accept_an_old_binary_insert_with_honest_defaults(
    owner_session: Session,
) -> None:
    """The expand migration must coexist with a pre-f912 API process."""

    entry_id = uuid.uuid4()
    row = (
        owner_session.connection()
        .execute(
            text(
                """
                INSERT INTO entries (id, clinic_id, patient_id, section, origin)
                VALUES (:id, :clinic_id, :patient_id, 'staff', 'human')
                RETURNING entry_type, occurred_at, created_at
                """
            ),
            {
                "id": entry_id,
                "clinic_id": demo_id("clinic-primary"),
                "patient_id": demo_id("patient-primary"),
            },
        )
        .mappings()
        .one()
    )

    assert row["entry_type"] == "legacy_review_required"
    assert row["occurred_at"] is not None
    assert row["occurred_at"] == row["created_at"]
