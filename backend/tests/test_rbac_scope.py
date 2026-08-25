import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.db import engine
from app.models import Entry
from app.seed import demo_id


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200
    return response.json()


def _patients(client: TestClient, headers: dict[str, str]) -> list[dict]:
    response = client.get("/api/v1/patients", headers=headers)
    assert response.status_code == 200
    return response.json()["data"]


def test_trust_fields_are_ignored_and_role_is_server_derived(
    client: TestClient, auth_headers
) -> None:
    staff_headers = auth_headers("staff")
    staff = _me(client, staff_headers)
    clinician = _me(client, auth_headers("clinician"))
    other = _me(client, auth_headers("other_staff"))
    patient_id = _patients(client, staff_headers)[0]["id"]

    response = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Trusted context",
            "content": "The server owns tenancy and authorship.",
            "patient_facing": False,
            "clinic_id": other["clinic_id"],
            "author_id": clinician["user_id"],
            "role": "clinician",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["clinic_id"] == staff["clinic_id"]
    assert body["author_id"] == staff["user_id"]

    denied = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Injected role",
            "content": "must not be created",
            "role": "clinician",
        },
    )
    assert denied.status_code == 403


def test_cross_clinic_resources_are_hidden_as_404(
    client: TestClient, auth_headers
) -> None:
    staff_headers = auth_headers("staff")
    other_headers = auth_headers("other_staff")
    primary_patient = _patients(client, staff_headers)[0]
    other_patient = _patients(client, other_headers)[0]

    assert (
        client.get(
            f"/api/v1/patients/{other_patient['id']}/timeline", headers=staff_headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/patients/{primary_patient['id']}/timeline", headers=other_headers
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/entries/{uuid.uuid4()}", headers=staff_headers).status_code
        == 404
    )


def test_patient_dto_and_query_exclude_internal_and_raw_ai(
    client: TestClient, auth_headers
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_headers = auth_headers("patient")
    patient_id = _patients(client, clinician_headers)[0]["id"]

    internal = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "INTERNAL-TITLE",
            "content": "INTERNAL-CONTENT",
            "patient_facing": False,
        },
    )
    assert internal.status_code == 201, internal.text
    visible = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Patient care plan",
            "content": "Take the prescribed medicine with food.",
            "patient_facing": True,
        },
    )
    assert visible.status_code == 201, visible.text

    timeline = client.get(
        f"/api/v1/patients/{patient_id}/timeline", headers=patient_headers
    )
    assert timeline.status_code == 200, timeline.text
    payload = timeline.json()
    rendered = str(payload)
    assert "INTERNAL-TITLE" not in rendered
    assert "INTERNAL-CONTENT" not in rendered
    assert "Patient care plan" in rendered
    forbidden_keys = {
        "comments",
        "raw_ai",
        "internal_comments",
        "internal_risk",
        "score_debug",
    }
    assert forbidden_keys.isdisjoint(payload.keys())
    for row in payload["data"]:
        assert forbidden_keys.isdisjoint(row.keys())

    other_patient = _patients(client, auth_headers("other_staff"))[0]
    assert (
        client.get(
            f"/api/v1/patients/{other_patient['id']}/timeline", headers=patient_headers
        ).status_code
        == 404
    )


def test_admin_cannot_edit_clinical_body_and_worker_is_system_only(
    client: TestClient, auth_headers
) -> None:
    staff_headers = auth_headers("staff")
    patient_id = _patients(client, staff_headers)[0]["id"]
    body = {
        "patient_id": patient_id,
        "section": "staff",
        "title": "Denied",
        "content": "Denied",
    }
    assert (
        client.post(
            "/api/v1/entries", headers=auth_headers("admin"), json=body
        ).status_code
        == 403
    )
    assert (
        client.get("/api/v1/patients", headers=auth_headers("admin")).status_code == 403
    )
    assert (
        client.get("/api/v1/patients", headers=auth_headers("worker")).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/entries", headers=auth_headers("worker"), json=body
        ).status_code
        == 403
    )

    system_body = body | {"section": "system", "origin": "system"}
    created = client.post(
        "/api/v1/entries", headers=auth_headers("worker"), json=system_body
    )
    assert created.status_code == 201, created.text
    assert created.json()["origin"] == "system"
    assert created.json()["patient_facing"] is False
    with Session(engine) as session:
        stored = session.get(Entry, created.json()["id"])
        assert stored is not None
        assert stored.source_job_id == demo_id("job-worker-demo")
