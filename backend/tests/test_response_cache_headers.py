import re
import uuid

from fastapi.testclient import TestClient


def _assert_private_no_store(response) -> None:  # type: ignore[no-untyped-def]
    directives = {part.strip() for part in response.headers["cache-control"].split(",")}
    assert {"private", "no-store"} <= directives
    assert response.headers["pragma"] == "no-cache"
    vary = {part.strip().lower() for part in response.headers["vary"].split(",")}
    assert {"cookie", "authorization", "origin"} <= vary


def test_authenticated_phi_and_auth_responses_are_never_cacheable(
    client: TestClient, auth_headers
) -> None:  # type: ignore[no-untyped-def]
    login = client.post("/api/v1/auth/demo-login", json={"persona": "clinician"})
    assert login.status_code == 200
    _assert_private_no_store(login)

    headers = auth_headers("clinician")
    me = client.get("/api/v1/auth/me", headers=headers)
    patients = client.get("/api/v1/patients", headers=headers)
    assert me.status_code == patients.status_code == 200
    patient = next(
        item
        for item in patients.json()["data"]
        if item["display_name"] == "Alex Synthetic"
    )
    patient_id = patient["id"]
    created = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Cache-header fixture",
            "content": "Synthetic clinical response body",
        },
    )
    assert created.status_code == 201, created.text
    _assert_private_no_store(created)
    timeline = client.get(f"/api/v1/patients/{patient_id}/timeline", headers=headers)
    glance = client.get(f"/api/v1/patients/{patient_id}/glance", headers=headers)
    assert timeline.status_code == glance.status_code == 200
    entry_id = created.json()["id"]
    entry = client.get(f"/api/v1/entries/{entry_id}", headers=headers)
    comments = client.get(f"/api/v1/entries/{entry_id}/comments", headers=headers)
    audit = client.get("/api/v1/admin/audit", headers=auth_headers("admin"))
    assert entry.status_code == comments.status_code == audit.status_code == 200

    voice = client.post(
        "/api/v1/voice/sessions",
        headers=headers,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": True,
            "fixture_id": "code-switch-overlap-v1",
        },
    )
    assert voice.status_code == 201, voice.text
    transcript = client.get(
        f"/api/v1/voice/sessions/{voice.json()['id']}/transcript", headers=headers
    )
    assert transcript.status_code == 404

    event_stream = client.get("/api/v1/events/stream?snapshot=true", headers=headers)
    assert event_stream.status_code == 200
    for response in (
        me,
        patients,
        timeline,
        glance,
        entry,
        comments,
        audit,
        voice,
        transcript,
        event_stream,
    ):
        _assert_private_no_store(response)

    missing = client.get(
        f"/api/v1/voice/sessions/{uuid.uuid4()}/transcript", headers=headers
    )
    assert missing.status_code == 404
    _assert_private_no_store(missing)


def test_html_shell_is_no_store_but_hashed_assets_are_immutable(
    client: TestClient,
) -> None:
    shell = client.get("/login", headers={"Accept": "text/html"})
    assert shell.status_code == 200
    assert shell.headers["content-type"].startswith("text/html")
    _assert_private_no_store(shell)

    match = re.search(r'(?:src|href)="(/assets/[^"]+)"', shell.text)
    assert match is not None
    asset = client.get(match.group(1))
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
