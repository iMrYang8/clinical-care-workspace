from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from app.core.db import engine
from app.main import app
from app.seed import reset_and_seed_demo_data


@pytest.fixture(autouse=True)
def seeded_database() -> Generator[None]:
    """Every test starts from the same synthetic, clinic-scoped dataset."""

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        reset_and_seed_demo_data(session)
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
