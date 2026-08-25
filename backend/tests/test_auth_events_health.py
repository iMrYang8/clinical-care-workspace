import uuid

from sqlmodel import Session

from app import initial_data
from app.api.routes import events as events_route
from app.core.config import settings
from app.core.db import engine
from app.models import ClinicMembership, DomainEvent
from app.seed import demo_id

CLINIC_HEADER = {"X-Clinic-ID": str(demo_id("clinic-primary"))}


def test_password_login_logout_health_and_bad_token(client) -> None:
    login = client.post(
        "/api/v1/auth/login",
        headers=CLINIC_HEADER,
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
            headers=CLINIC_HEADER,
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
            headers=CLINIC_HEADER,
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
        headers=CLINIC_HEADER,
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
    stream = client.get("/api/v1/events/stream?snapshot=true", headers=headers)
    assert stream.status_code == 200
    assert "event: entry.created" in stream.text
    event_id = next(
        int(line.removeprefix("id: "))
        for line in stream.text.splitlines()
        if line.startswith("id: ")
    )
    resumed = client.get(
        "/api/v1/events/stream?snapshot=true",
        headers=headers | {"Last-Event-ID": str(event_id)},
    )
    assert "event: entry.created" not in resumed.text
    assert "event: caught-up" in resumed.text

    for persona in ("patient", "admin", "worker"):
        denied = client.get(
            "/api/v1/events/stream?snapshot=true", headers=auth_headers(persona)
        )
        assert denied.status_code == 403


def test_demo_auth_is_explicitly_development_only(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_DEMO_AUTH", False)
    response = client.post("/api/v1/auth/demo-login", json={"persona": "admin"})
    assert response.status_code == 404


def test_entry_routes_publish_concrete_openapi_response_schemas(client) -> None:
    document = client.get("/api/v1/openapi.json").json()
    create_schema = document["paths"]["/api/v1/entries"]["post"]["responses"]["201"][
        "content"
    ]["application/json"]["schema"]
    read_schema = document["paths"]["/api/v1/entries/{entry_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert create_schema.get("anyOf")
    assert read_schema.get("anyOf")


def test_live_sse_generator_polls_and_emits_heartbeats(monkeypatch) -> None:
    clinic_id = uuid.uuid4()
    event = DomainEvent(
        sequence_no=42,
        clinic_id=clinic_id,
        event_type="entry.updated",
        aggregate_type="entry",
        aggregate_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
    )
    pages = iter([[], [event]])
    sleeps: list[float] = []
    monkeypatch.setattr(
        events_route, "_load_events", lambda _clinic, _after: next(pages)
    )
    monkeypatch.setattr(events_route.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(events_route.time, "sleep", sleeps.append)
    frames = events_route._frames(clinic_id, 0, snapshot=False)
    assert next(frames).startswith("id: 42\nevent: entry.updated")
    assert sleeps == [events_route.POLL_INTERVAL_SECONDS]
    frames.close()

    ticks = iter([0.0, events_route.HEARTBEAT_INTERVAL_SECONDS + 1])
    monkeypatch.setattr(events_route, "_load_events", lambda _clinic, _after: [])
    monkeypatch.setattr(events_route.time, "monotonic", lambda: next(ticks))
    heartbeats = events_route._frames(clinic_id, 42, snapshot=False)
    assert next(heartbeats) == ": heartbeat\n\n"
    heartbeats.close()


def test_initial_data_is_idempotent() -> None:
    initial_data.main()
    initial_data.main()
