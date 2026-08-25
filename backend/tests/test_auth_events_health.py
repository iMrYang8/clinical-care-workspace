from sqlmodel import Session

from app import initial_data
from app.core.db import engine
from app.models import ClinicMembership
from app.seed import demo_id


def test_password_login_logout_health_and_bad_token(client) -> None:
    login = client.post(
        "/api/v1/auth/login",
        data={
            "username": "staff@nightingale.synthetic",
            "password": "synthetic-demo-only",
        },
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/utils/health-check/").json() is True
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 403
    )

    assert (
        client.post(
            "/api/v1/auth/login",
            data={
                "username": "staff@nightingale.synthetic",
                "password": "wrong-password",
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            data={
                "username": "missing@nightingale.synthetic",
                "password": "wrong-password",
            },
        ).status_code
        == 400
    )


def test_password_login_requires_active_membership(client) -> None:
    with Session(engine) as session:
        membership = session.get(ClinicMembership, demo_id("membership-staff"))
        assert membership is not None
        membership.is_active = False
        session.add(membership)
        session.commit()
    response = client.post(
        "/api/v1/auth/login",
        data={
            "username": "staff@nightingale.synthetic",
            "password": "synthetic-demo-only",
        },
    )
    assert response.status_code == 403


def test_sse_resumes_after_last_event_id(client, auth_headers) -> None:
    headers = auth_headers("staff")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    created = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "SSE",
            "content": "event source",
        },
    )
    assert created.status_code == 201
    stream = client.get("/api/v1/events/stream", headers=headers)
    assert stream.status_code == 200
    assert "event: entry.created" in stream.text
    event_id = next(
        int(line.removeprefix("id: "))
        for line in stream.text.splitlines()
        if line.startswith("id: ")
    )
    resumed = client.get(
        "/api/v1/events/stream",
        headers=headers | {"Last-Event-ID": str(event_id)},
    )
    assert "event: entry.created" not in resumed.text
    assert "event: caught-up" in resumed.text


def test_initial_data_is_idempotent() -> None:
    initial_data.main()
    initial_data.main()
