from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session

from app.core.db import engine
from app.main import app
from app.seed import seed_demo_data


def reset_synthetic_fixture(session: Session) -> None:
    """Reset only the dedicated local test fixture before each contract test."""

    session.connection().execute(
        text(
            """
            TRUNCATE TABLE
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

    with Session(engine) as session:
        reset_synthetic_fixture(session)
    yield


@pytest.fixture()
def client() -> Generator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def headers_for(client: TestClient, persona: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/demo-login", json={"persona": persona})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def auth_headers(client: TestClient):
    return lambda persona: headers_for(client, persona)
