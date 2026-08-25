import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session, select

from app.core.db import engine
from app.models import Comment, EntryVersion, ProvenancePointer
from app.seed import demo_id

TENANT_TABLES = {
    "clinic_memberships",
    "patients",
    "patient_user_links",
    "entries",
    "entry_versions",
    "entry_relations",
    "comments",
    "comment_mentions",
    "care_tasks",
    "highlights",
    "provenance_pointers",
    "conflict_cases",
    "audit_events",
    "patient_glance_snapshots",
    "domain_events",
}


def _make_anchored_comment(
    client: TestClient, headers: dict[str, str]
) -> tuple[dict, dict]:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry_response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Constraint fixture",
            "content": "anchor",
        },
    )
    assert entry_response.status_code == 201, entry_response.text
    entry = entry_response.json()
    comment_response = client.post(
        f"/api/v1/entries/{entry['id']}/comments",
        headers=headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": 6,
            "exact_quote": "anchor",
            "body": "tenant constraint",
        },
    )
    assert comment_response.status_code == 201, comment_response.text
    return entry, comment_response.json()


def test_migration_installs_rls_composite_constraints_and_immutability() -> None:
    with engine.connect() as connection:
        policies = {
            row.tablename
            for row in connection.execute(
                text(
                    """
                    SELECT tablename FROM pg_policies
                    WHERE schemaname = current_schema()
                      AND policyname = 'clinic_isolation'
                    """
                )
            )
        }
        triggers = {
            row.tgname
            for row in connection.execute(
                text(
                    """
                    SELECT tgname FROM pg_trigger
                    WHERE NOT tgisinternal
                      AND tgname IN (
                        'trg_entry_version_append_only',
                        'trg_provenance_append_only'
                      )
                    """
                )
            )
        }
    assert TENANT_TABLES <= policies
    assert triggers == {
        "trg_entry_version_append_only",
        "trg_provenance_append_only",
    }

    inspector = inspect(engine)
    membership_uniques = {
        item["name"] for item in inspector.get_unique_constraints("clinic_memberships")
    }
    assert "uq_membership_clinic_id" in membership_uniques
    expected_fks = {
        "comments": {"fk_comment_parent_tenant", "fk_comment_assignment_tenant"},
        "comment_mentions": {"fk_comment_mention_tenant"},
        "care_tasks": {"fk_task_comment_tenant", "fk_task_assignee_tenant"},
        "provenance_pointers": {
            "fk_provenance_highlight_tenant",
            "fk_provenance_comment_tenant",
        },
    }
    for table, expected in expected_fks.items():
        actual = {item["name"] for item in inspector.get_foreign_keys(table)}
        assert expected <= actual


def test_cross_clinic_composite_fk_and_append_only_triggers(
    client: TestClient, auth_headers
) -> None:
    entry, comment = _make_anchored_comment(client, auth_headers("staff"))

    with Session(engine) as session:
        stored_comment = session.get(Comment, uuid.UUID(comment["id"]))
        assert stored_comment is not None
        stored_comment.assigned_membership_id = demo_id("membership-other_staff")
        session.add(stored_comment)
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        version = session.get(EntryVersion, uuid.UUID(entry["version_id"]))
        assert version is not None
        version.content_sha256 = "0" * 64
        session.add(version)
        with pytest.raises(DBAPIError, match="immutable entry_version"):
            session.commit()

    with Session(engine) as session:
        pointer = session.exec(
            select(ProvenancePointer).where(
                ProvenancePointer.comment_id == uuid.UUID(comment["id"])
            )
        ).one()
        pointer.review_required = True
        session.add(pointer)
        with pytest.raises(DBAPIError, match="append-only"):
            session.commit()
