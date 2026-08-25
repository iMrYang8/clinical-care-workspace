import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import initial_data
from app.api.deps import get_detached_request_context
from app.api.routes import events as events_route
from app.core.config import settings
from app.core.db import engine
from app.main import app
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
    monkeypatch.setattr(events_route, "monotonic", lambda: 0.0)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(events_route, "sleep", fake_sleep)

    async def exercise() -> None:
        frames = events_route._frames(clinic_id, 0, snapshot=False)
        assert (await anext(frames)).startswith("id: 42\nevent: entry.updated")
        assert sleeps == [events_route.POLL_INTERVAL_SECONDS]
        await frames.aclose()

        ticks = iter([0.0, events_route.HEARTBEAT_INTERVAL_SECONDS + 1])
        monkeypatch.setattr(events_route, "_load_events", lambda _clinic, _after: [])
        monkeypatch.setattr(events_route, "monotonic", lambda: next(ticks))
        heartbeats = events_route._frames(clinic_id, 42, snapshot=False)
        assert await anext(heartbeats) == ": heartbeat\n\n"
        await heartbeats.aclose()

    asyncio.run(exercise())


def test_sse_auth_and_concurrent_snapshots_release_pool_connections(client) -> None:
    login = client.post("/api/v1/auth/demo-login", json={"persona": "staff"})
    token = login.json()["access_token"]
    checked_out = engine.pool.checkedout
    baseline = checked_out()
    context = get_detached_request_context(token, None)
    assert context.role == "staff"
    assert checked_out() == baseline

    async def drain_snapshots() -> None:
        async def drain_one() -> list[str]:
            return [
                frame
                async for frame in events_route._frames(
                    context.clinic_id, 0, snapshot=True
                )
            ]

        snapshots = await asyncio.gather(*(drain_one() for _ in range(20)))
        assert all(frames[-1].startswith("event: caught-up") for frames in snapshots)

    asyncio.run(drain_snapshots())
    assert checked_out() == baseline


def test_browser_cookie_flags_cookie_auth_logout_and_csrf() -> None:
    with TestClient(app, base_url="https://testserver") as browser:
        login = browser.post("/api/v1/auth/demo-login", json={"persona": "staff"})
        assert login.status_code == 200
        set_cookie = login.headers["set-cookie"]
        assert "nightingale_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert browser.get("/api/v1/auth/me").status_code == 200

        patient_id = browser.get("/api/v1/patients").json()["data"][0]["id"]
        denied = browser.post(
            "/api/v1/entries",
            json={
                "patient_id": patient_id,
                "section": "staff",
                "title": "CSRF denied",
                "content": "No trusted browser origin",
            },
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "CSRF origin rejected"

        allowed = browser.post(
            "/api/v1/entries",
            headers={"Origin": str(settings.FRONTEND_HOST).rstrip("/")},
            json={
                "patient_id": patient_id,
                "section": "staff",
                "title": "Cookie mutation",
                "content": "Trusted same-origin request",
            },
        )
        assert allowed.status_code == 201, allowed.text

        logout = browser.post(
            "/api/v1/auth/logout",
            headers={"Origin": str(settings.FRONTEND_HOST).rstrip("/")},
        )
        assert logout.status_code == 200
        assert 'nightingale_session=""' in logout.headers["set-cookie"]
        assert browser.get("/api/v1/auth/me").status_code == 401


def test_logout_clears_an_invalid_browser_cookie_when_same_origin() -> None:
    """An expired/corrupt HttpOnly cookie must not trap a shared browser."""

    with TestClient(app, base_url="https://testserver") as browser:
        logout = browser.post(
            "/api/v1/auth/logout",
            headers={
                "Cookie": f"{settings.AUTH_COOKIE_NAME}=invalid-token",
                "Origin": str(settings.FRONTEND_HOST).rstrip("/"),
            },
        )

        assert logout.status_code == 200
        assert f'{settings.AUTH_COOKIE_NAME}=""' in logout.headers["set-cookie"]


def test_logout_rejects_csrf_without_claiming_cookie_deletion_then_retries() -> None:
    with TestClient(app, base_url="https://testserver") as browser:
        login = browser.post("/api/v1/auth/demo-login", json={"persona": "staff"})
        assert login.status_code == 200

        rejected = browser.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://csrf.invalid"},
        )
        assert rejected.status_code == 403
        assert "set-cookie" not in rejected.headers
        assert browser.get("/api/v1/auth/me").status_code == 200

        retried = browser.post(
            "/api/v1/auth/logout",
            headers={"Origin": str(settings.FRONTEND_HOST).rstrip("/")},
        )
        assert retried.status_code == 200
        assert browser.get("/api/v1/auth/me").status_code == 401

        repeated = browser.post(
            "/api/v1/auth/logout",
            headers={"Origin": str(settings.FRONTEND_HOST).rstrip("/")},
        )
        assert repeated.status_code == 200


def test_bearer_mutation_remains_available_without_browser_origin(
    client, auth_headers
) -> None:
    headers = auth_headers("staff")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Bearer API",
            "content": "Explicit non-browser transport",
        },
    )
    assert response.status_code == 201


def test_initial_data_is_idempotent() -> None:
    initial_data.main()
    initial_data.main()
