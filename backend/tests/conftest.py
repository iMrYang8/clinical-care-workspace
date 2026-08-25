from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlmodel import Session

from app.core.config import settings
from app.core.db import engine
from app.main import app
from app.seed import demo_id, seed_demo_data

# Fixture lifecycle uses the migration owner; application requests still use the
# restricted engine imported above.  Keeping these credentials separate makes
# RLS observable in the normal API/test path while retaining deterministic reset.
migration_engine = create_engine(
    str(settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL)
)


@event.listens_for(engine, "begin")
def _default_direct_test_session_to_primary_clinic(connection) -> None:
    """Scope legacy direct test inspections without granting an RLS bypass."""

    if connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT set_config('app.current_clinic_id', :clinic_id, true)"),
            {"clinic_id": str(demo_id("clinic-primary"))},
        )


def reset_synthetic_fixture(session: Session) -> None:
    """Reset only the dedicated local test fixture before each contract test."""

    session.connection().execute(
        text(
            """
            TRUNCATE TABLE
              clinical_facts, transcript_segments, transcript_revisions,
              audio_assets, audio_chunks, voice_devices, voice_sessions,
              domain_events, audit_events, job_attempts, ai_runs, redaction_runs,
              importance_feedback_events, importance_feature_stats, decay_runs,
              retention_locks, archive_blobs, provenance_pointers, conflict_cases,
              highlights, care_tasks, comment_mentions, comments, entry_relations,
              entry_versions, entries, jobs, patient_glance_snapshots, patient_user_links,
              patients, clinic_memberships, users, clinics
            RESTART IDENTITY CASCADE
            """
        )
    )
    session.commit()
    seed_demo_data(session)


@pytest.fixture(autouse=True)
def seeded_database(request: pytest.FixtureRequest) -> Generator[None]:
    """Every test starts from the same synthetic, clinic-scoped dataset."""

    if request.node.get_closest_marker("unit") is not None:
        yield
        return

    with Session(migration_engine) as session:
        reset_synthetic_fixture(session)
    yield


@pytest.fixture()
def client() -> Generator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def owner_session() -> Generator[Session]:
    """Migration-owner session for DDL/integrity assertions, never app paths."""

    with Session(migration_engine) as session:
        yield session
        session.rollback()


def headers_for(client: TestClient, persona: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/demo-login", json={"persona": persona})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def auth_headers(client: TestClient):
    return lambda persona: headers_for(client, persona)
