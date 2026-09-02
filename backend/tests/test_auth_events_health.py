import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session

from app import initial_data
from app.api.deps import get_detached_request_context
from app.api.routes import events as events_route
from app.core.config import settings
from app.core.db import engine
from app.main import app
from app.models import ClinicMembership, DomainEvent, MembershipInvitationAccept, User
from app.seed import demo_id

CLINIC_HEADER = {"X-Clinic-Code": "NIGHTINGALE"}


def test_password_login_logout_health_and_bad_token(client) -> None:
    login = client.post(
        "/api/v1/auth/login",
        headers=CLINIC_HEADER,
        data={
            "username": "staff@nightingale.example",
            "password": "synthetic-demo-only",
        },
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["clinic_code"] == "NIGHTINGALE"
    assert me.json()["clinic_name"] == "Nightingale Clinic"
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/utils/health-check/").json() is True
    invalid = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer invalid"})
    assert invalid.status_code == 403
    assert invalid.headers["X-Nightingale-Session-Invalid"] == "1"

    assert (
        client.post(
            "/api/v1/auth/login",
            headers=CLINIC_HEADER,
            data={
                "username": "staff@nightingale.example",
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
                "username": "missing@nightingale.example",
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
            "username": "staff@nightingale.example",
            "password": "synthetic-demo-only",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Incorrect clinic code, email, or password"


def test_password_login_normalizes_clinic_code_and_email_and_unifies_failures(
    client,
) -> None:
    successful = client.post(
        "/api/v1/auth/login",
        headers={"X-Clinic-Code": "nightingale"},
        data={
            "username": "  STAFF@NIGHTINGALE.EXAMPLE  ",
            "password": "synthetic-demo-only",
        },
    )
    assert successful.status_code == 200, successful.text

    attempts = (
        (
            {"X-Clinic-Code": "UNKNOWN"},
            "staff@nightingale.example",
            "synthetic-demo-only",
        ),
        (
            {"X-Clinic-Code": "NIGHTINGALE"},
            "missing@nightingale.example",
            "synthetic-demo-only",
        ),
        (
            {"X-Clinic-Code": "NIGHTINGALE"},
            "staff@nightingale.example",
            "wrong-password",
        ),
        (
            {"X-Clinic-Code": "NTU-01"},
            "staff@nightingale.example",
            "synthetic-demo-only",
        ),
        (
            {"X-Clinic-Code": " NIGHTINGALE "},
            "staff@nightingale.example",
            "synthetic-demo-only",
        ),
        (
            {"X-Clinic-Code": "OTHERCLINIC"},
            "staff@nightingale.example",
            "synthetic-demo-only",
        ),
        (
            {"X-Clinic-Code": "NIGHTINGALE"},
            "staff@nightingale.example",
            "x" * 201,
        ),
    )
    errors = []
    for headers, username, password in attempts:
        response = client.post(
            "/api/v1/auth/login",
            headers=headers,
            data={"username": username, "password": password},
        )
        errors.append((response.status_code, response.json()["detail"]))
    assert set(errors) == {
        (400, "Incorrect clinic code, email, or password"),
    }


def test_password_login_uses_the_same_error_for_an_inactive_user(
    client, owner_session
) -> None:
    user = owner_session.get(User, demo_id("user-staff"))
    assert user is not None
    user.is_active = False
    owner_session.add(user)
    owner_session.commit()

    response = client.post(
        "/api/v1/auth/login",
        headers=CLINIC_HEADER,
        data={
            "username": "staff@nightingale.example",
            "password": "synthetic-demo-only",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Incorrect clinic code, email, or password"


def test_password_login_rejects_worker_membership_with_the_generic_error(
    client,
) -> None:
    response = client.post(
        "/api/v1/auth/login",
        headers=CLINIC_HEADER,
        data={
            "username": "worker@nightingale.example",
            "password": "synthetic-demo-only",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Incorrect clinic code, email, or password"
    assert settings.AUTH_COOKIE_NAME not in response.cookies


def test_invitation_password_schema_preserves_passphrases_and_enforces_bounds() -> None:
    for length in (16, 200):
        password = " " + "x" * (length - 1)
        body = MembershipInvitationAccept(
            email="clinician@example.com",
            token="t" * 64,
            password=password,
        )
        assert body.password == password
    for length in (15, 201):
        with pytest.raises(ValidationError):
            MembershipInvitationAccept(
                email="clinician@example.com",
                token="t" * 64,
                password="x" * length,
            )


def test_inactive_authenticated_membership_marks_session_invalid(
    client, auth_headers
) -> None:
    headers = auth_headers("staff")
    with Session(engine) as session:
        membership = session.get(ClinicMembership, demo_id("membership-staff"))
        assert membership is not None
        membership.is_active = False
        session.add(membership)
        session.commit()

    response = client.get("/api/v1/auth/me", headers=headers)

    assert response.status_code == 403
    # Strict identity RLS hides an inactive membership before the application
    # can distinguish it from any other invalid membership context.
    assert response.json()["detail"] == "Invalid membership context"
    assert response.headers["X-Nightingale-Session-Invalid"] == "1"


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
    user_id = uuid.uuid4()
    membership_id = uuid.uuid4()
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
        events_route,
        "_load_events",
        lambda _user, _membership, _clinic, _after: next(pages),
    )
    monkeypatch.setattr(events_route, "monotonic", lambda: 0.0)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(events_route, "sleep", fake_sleep)

    async def exercise() -> None:
        frames = events_route._frames(
            user_id,
            membership_id,
            clinic_id,
            0,
            expires_at_epoch=float("inf"),
            snapshot=False,
        )
        assert (await anext(frames)).startswith("id: 42\nevent: entry.updated")
        assert sleeps == [events_route.POLL_INTERVAL_SECONDS]
        await frames.aclose()

        ticks = iter([0.0, events_route.HEARTBEAT_INTERVAL_SECONDS + 1])
        monkeypatch.setattr(
            events_route,
            "_load_events",
            lambda _user, _membership, _clinic, _after: [],
        )
        monkeypatch.setattr(events_route, "monotonic", lambda: next(ticks))
        heartbeats = events_route._frames(
            user_id,
            membership_id,
            clinic_id,
            42,
            expires_at_epoch=float("inf"),
            snapshot=False,
        )
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
    assert context.token_expires_at_epoch is not None
    assert checked_out() == baseline

    async def drain_snapshots() -> None:
        async def drain_one() -> list[str]:
            return [
                frame
                async for frame in events_route._frames(
                    context.user_id,
                    context.membership.id,
                    context.clinic_id,
                    0,
                    expires_at_epoch=context.token_expires_at_epoch,
                    snapshot=True,
                )
            ]

        snapshots = await asyncio.gather(*(drain_one() for _ in range(20)))
        assert all(frames[-1].startswith("event: caught-up") for frames in snapshots)

    asyncio.run(drain_snapshots())
    assert checked_out() == baseline


def test_sse_rechecks_membership_and_ends_before_post_revocation_events(
    client, auth_headers, owner_session, monkeypatch
) -> None:
    headers = auth_headers("staff")
    login = client.post("/api/v1/auth/demo-login", json={"persona": "staff"})
    context = get_detached_request_context(login.json()["access_token"], None)
    before_revocation = DomainEvent(
        clinic_id=context.clinic_id,
        event_type="entry.updated",
        aggregate_type="entry",
        aggregate_id=uuid.uuid4(),
        actor_id=context.user_id,
        payload_json={"before_revocation": True},
    )
    owner_session.add(before_revocation)
    owner_session.commit()
    owner_session.refresh(before_revocation)
    assert before_revocation.sequence_no is not None

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(events_route, "sleep", no_wait)

    async def drain_revoked() -> None:
        # The first page is authorized and proves the stream was live before
        # the membership transition.
        frames = events_route._frames(
            context.user_id,
            context.membership.id,
            context.clinic_id,
            before_revocation.sequence_no - 1,
            expires_at_epoch=context.token_expires_at_epoch,
            snapshot=False,
        )
        first = await anext(frames)
        assert "before_revocation" in first

        membership = owner_session.get(ClinicMembership, context.membership.id)
        assert membership is not None
        membership.is_active = False
        owner_session.add(membership)
        owner_session.commit()
        owner_session.add(
            DomainEvent(
                clinic_id=context.clinic_id,
                event_type="entry.updated",
                aggregate_type="entry",
                aggregate_id=uuid.uuid4(),
                actor_id=context.user_id,
                payload_json={"must_not_leak": True},
            )
        )
        owner_session.commit()

        terminal = await anext(frames)
        assert terminal == "event: session.revoked\ndata: {}\n\n"
        assert "must_not_leak" not in terminal
        with pytest.raises(StopAsyncIteration):
            await anext(frames)

    asyncio.run(drain_revoked())
    rejected = client.get("/api/v1/auth/me", headers=headers)
    assert rejected.status_code == 403
    assert rejected.headers["X-Nightingale-Session-Invalid"] == "1"


@pytest.mark.unit
def test_sse_ends_at_original_token_expiry_before_loading_later_events(
    monkeypatch,
) -> None:
    user_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    clinic_id = uuid.uuid4()
    before_expiry = DomainEvent(
        sequence_no=41,
        clinic_id=clinic_id,
        event_type="entry.updated",
        aggregate_type="entry",
        aggregate_id=uuid.uuid4(),
        actor_id=user_id,
        payload_json={"before_expiry": True},
    )
    must_not_leak = DomainEvent(
        sequence_no=42,
        clinic_id=clinic_id,
        event_type="entry.updated",
        aggregate_type="entry",
        aggregate_id=uuid.uuid4(),
        actor_id=user_id,
        payload_json={"must_not_leak_after_expiry": True},
    )
    now = [1_000.0]
    loads: list[int] = []

    def load_page(
        _user_id: uuid.UUID,
        _membership_id: uuid.UUID,
        _clinic_id: uuid.UUID,
        after: int,
    ) -> list[DomainEvent]:
        loads.append(after)
        return [before_expiry] if after == 0 else [must_not_leak]

    async def cross_expiry(_delay: float) -> None:
        now[0] = 1_001.0

    monkeypatch.setattr(events_route, "_load_events", load_page)
    monkeypatch.setattr(events_route, "wall_time", lambda: now[0])
    monkeypatch.setattr(events_route, "sleep", cross_expiry)

    async def exercise() -> None:
        frames = events_route._frames(
            user_id,
            membership_id,
            clinic_id,
            0,
            expires_at_epoch=1_001.0,
            snapshot=False,
        )
        first = await anext(frames)
        assert "before_expiry" in first

        terminal = await anext(frames)
        assert terminal == "event: session.revoked\ndata: {}\n\n"
        assert "must_not_leak_after_expiry" not in terminal
        with pytest.raises(StopAsyncIteration):
            await anext(frames)

    asyncio.run(exercise())
    # Expiry is checked before the next tenant read, so the post-expiry page is
    # neither queried nor serialized onto the established connection.
    assert loads == [0]


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
