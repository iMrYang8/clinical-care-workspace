import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session, select

from app.core.db import engine, set_rls_clinic
from app.models import Comment, EntryVersion, Highlight, ProvenancePointer
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
    "jobs",
    "job_attempts",
    "redaction_runs",
    "ai_runs",
    "importance_feedback_events",
    "importance_feature_stats",
    "archive_blobs",
    "decay_runs",
    "retention_locks",
}


def _set_local_clinic(connection, clinic_id: uuid.UUID) -> None:
    connection.execute(
        text("SELECT set_config('app.current_clinic_id', :clinic_id, true)"),
        {"clinic_id": str(clinic_id)},
    )


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
                        'trg_provenance_append_only',
                        'trg_highlight_anchor_guard'
                      )
                    """
                )
            )
        }
    assert TENANT_TABLES <= policies
    assert triggers == {
        "trg_entry_version_append_only",
        "trg_provenance_append_only",
        "trg_highlight_anchor_guard",
    }

    inspector = inspect(engine)
    membership_uniques = {
        item["name"] for item in inspector.get_unique_constraints("clinic_memberships")
    }
    assert "uq_membership_clinic_id" in membership_uniques
    expected_fks = {
        "entries": {"fk_entry_source_job_tenant"},
        "entry_versions": {"fk_version_archive_blob_tenant"},
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


def test_runtime_login_is_restricted_non_owner_and_rls_is_enforced() -> None:
    primary_clinic = demo_id("clinic-primary")
    other_clinic = demo_id("clinic-other")
    primary_patient = demo_id("patient-primary")
    other_patient = demo_id("patient-other")

    with engine.begin() as connection:
        role = connection.execute(
            text(
                """
                SELECT current_user AS role_name, rolcanlogin, rolsuper,
                       rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
                FROM pg_roles WHERE rolname = current_user
                """
            )
        ).one()
        owner = connection.execute(
            text(
                """
                SELECT tableowner FROM pg_tables
                WHERE schemaname = current_schema() AND tablename = 'patients'
                """
            )
        ).scalar_one()
        privileges = connection.execute(
            text(
                """
                SELECT
                  has_schema_privilege(current_user, current_schema(), 'CREATE')
                    AS can_create_schema_objects,
                  has_table_privilege(current_user, 'patients', 'SELECT')
                    AS can_select_patients,
                  has_table_privilege(current_user, 'patients', 'TRUNCATE')
                    AS can_truncate_patients,
                  has_table_privilege(current_user, 'alembic_version', 'UPDATE')
                    AS can_mutate_migration_history
                """
            )
        ).one()

    assert role.role_name == "nightingale_app"
    assert role.rolcanlogin is True
    assert role.rolsuper is False
    assert role.rolcreatedb is False
    assert role.rolcreaterole is False
    assert role.rolreplication is False
    assert role.rolbypassrls is False
    assert owner != role.role_name
    assert privileges.can_create_schema_objects is False
    assert privileges.can_select_patients is True
    assert privileges.can_truncate_patients is False
    assert privileges.can_mutate_migration_history is False

    # SET LOCAL is intentionally pool-safe; the Session hook reapplies the
    # trusted value when application code commits and then refreshes/queries.
    with Session(engine) as session:
        set_rls_clinic(session, other_clinic)
        other_visible = (
            session.execute(text("SELECT id FROM patients ORDER BY id")).scalars().all()
        )
        assert other_patient in other_visible
        assert primary_patient not in other_visible
        session.commit()
        after_commit = session.execute(text("SELECT id FROM patients")).scalars().all()
        assert other_patient in after_commit
        assert primary_patient not in after_commit

    with engine.connect() as connection:
        transaction = connection.begin()
        _set_local_clinic(connection, primary_clinic)
        visible_ids = set(
            connection.execute(text("SELECT id FROM patients")).scalars().all()
        )
        assert primary_patient in visible_ids
        assert other_patient not in visible_ids

        allowed_patient_id = uuid.uuid4()
        connection.execute(
            text(
                """
                INSERT INTO patients (
                  id, clinic_id, display_name_ciphertext, external_ref_hash
                ) VALUES (:id, :clinic_id, :ciphertext, :external_ref_hash)
                """
            ),
            {
                "id": allowed_patient_id,
                "clinic_id": primary_clinic,
                "ciphertext": b"synthetic-rls-test",
                "external_ref_hash": "a" * 64,
            },
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM patients WHERE id = :id"),
                {"id": allowed_patient_id},
            ).scalar_one()
            == 1
        )
        transaction.rollback()

    with pytest.raises(DBAPIError, match="row-level security policy"):
        with engine.begin() as connection:
            _set_local_clinic(connection, primary_clinic)
            connection.execute(
                text(
                    """
                    INSERT INTO patients (
                      id, clinic_id, display_name_ciphertext, external_ref_hash
                    ) VALUES (:id, :clinic_id, :ciphertext, :external_ref_hash)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "clinic_id": other_clinic,
                    "ciphertext": b"cross-clinic-denied",
                    "external_ref_hash": "b" * 64,
                },
            )


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
        version.content_ciphertext = b"tampered-in-place"
        session.add(version)
        with pytest.raises(DBAPIError, match="immutable entry_version payload"):
            session.commit()

    with Session(engine) as session:
        version = session.get(EntryVersion, uuid.UUID(entry["version_id"]))
        assert version is not None
        version.title_ciphertext = b"tampered-title-in-place"
        session.add(version)
        with pytest.raises(DBAPIError, match="immutable entry_version payload"):
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


def test_highlight_anchor_fields_are_database_immutable(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Highlight guard",
            "content": "anchored",
        },
    ).json()
    highlight = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": 8,
            "exact_quote": "anchored",
            "label": "Immutable anchor",
        },
    )
    assert highlight.status_code == 201, highlight.text

    with Session(engine) as session:
        stored = session.get(Highlight, uuid.UUID(highlight.json()["id"]))
        assert stored is not None
        stored.entry_id = uuid.uuid4()
        session.add(stored)
        with pytest.raises(DBAPIError, match="immutable highlight anchor"):
            session.commit()
