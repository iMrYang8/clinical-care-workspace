import hashlib
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core import security
from app.core.config import settings
from app.core.db import engine
from app.core.field_crypto import field_codec
from app.models import Entry, Patient
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
    assert "X-Nightingale-Session-Invalid" not in denied.headers


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


def test_signed_clinic_claim_is_verified_against_live_membership(
    client: TestClient,
) -> None:
    login = client.post("/api/v1/auth/demo-login", json={"persona": "staff"})
    token = login.json()["access_token"]
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[security.ALGORITHM])
    payload["clinic_id"] = str(demo_id("clinic-other"))
    mismatched = jwt.encode(payload, settings.SECRET_KEY, algorithm=security.ALGORITHM)
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {mismatched}"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid membership context"
    assert response.headers["X-Nightingale-Session-Invalid"] == "1"


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

    patient_created = client.post(
        "/api/v1/entries",
        headers=patient_headers,
        json={
            "patient_id": patient_id,
            "section": "patient",
            "title": "My observation",
            "content": "Synthetic patient insight",
            "clinic_id": str(uuid.uuid4()),
            "author_id": str(uuid.uuid4()),
            "role": "clinician",
        },
    )
    assert patient_created.status_code == 201, patient_created.text
    patient_payload = patient_created.json()
    assert "clinic_id" not in patient_payload
    assert "author_id" not in patient_payload
    assert "origin" not in patient_payload
    read_back = client.get(
        f"/api/v1/entries/{patient_payload['id']}", headers=patient_headers
    )
    assert read_back.status_code == 200
    assert "author_id" not in read_back.json()


def test_admin_cannot_edit_clinical_body_and_worker_is_system_only(
    client: TestClient, auth_headers, owner_session: Session
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
    clinician_headers = auth_headers("clinician")
    created = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Admin oversight fixture",
            "content": "prefix IMPORTANT suffix",
            "patient_facing": True,
        },
    )
    assert created.status_code == 201, created.text
    observed_entry = created.json()
    highlight = client.post(
        f"/api/v1/entries/{observed_entry['id']}/highlights",
        headers=clinician_headers,
        json={
            "entry_version_id": observed_entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Reviewed source",
            "patient_facing": True,
        },
    )
    assert highlight.status_code == 201, highlight.text
    assert (
        client.post(
            f"/api/v1/highlights/{highlight.json()['id']}/accept",
            headers=clinician_headers,
        ).status_code
        == 200
    )
    admin_headers = auth_headers("admin")
    admin_patients = client.get("/api/v1/patients", headers=admin_headers)
    assert admin_patients.status_code == 200
    assert patient_id in {item["id"] for item in admin_patients.json()["data"]}
    admin_timeline = client.get(
        f"/api/v1/patients/{patient_id}/timeline", headers=admin_headers
    )
    assert admin_timeline.status_code == 200
    assert observed_entry["id"] in {
        item["id"] for item in admin_timeline.json()["data"]
    }
    entry_read = client.get(
        f"/api/v1/entries/{observed_entry['id']}", headers=admin_headers
    )
    assert entry_read.status_code == 200
    assert (
        client.get(
            f"/api/v1/entries/{observed_entry['id']}/versions",
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/entries/{observed_entry['id']}/comments",
            headers=admin_headers,
        ).status_code
        == 200
    )
    admin_glance = client.get(
        f"/api/v1/patients/{patient_id}/glance", headers=admin_headers
    )
    assert admin_glance.status_code == 200
    pointer_id = admin_glance.json()["cards"][0]["provenance_pointer_id"]
    assert (
        client.get(
            f"/api/v1/provenance/{pointer_id}/resolve", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/entries/{observed_entry['id']}",
            headers={**admin_headers, "If-Match": entry_read.headers["etag"]},
            json={"content": "Admin must remain read-only"},
        ).status_code
        == 403
    )
    other_patient_id = _patients(client, auth_headers("other_staff"))[0]["id"]
    assert (
        client.get(
            f"/api/v1/patients/{other_patient_id}/timeline", headers=admin_headers
        ).status_code
        == 404
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

    second_patient_id = uuid.uuid4()
    clinic_id = demo_id("clinic-primary")
    owner_session.add(
        Patient(
            id=second_patient_id,
            clinic_id=clinic_id,
            display_name_ciphertext=field_codec.encrypt_text(
                clinic_id,
                "patient.display_name",
                second_patient_id,
                "Second Synthetic",
            ),
            external_ref_hash=hashlib.sha256(b"SYNTHETIC-SECOND").hexdigest(),
        )
    )
    owner_session.commit()
    cross_patient = client.post(
        "/api/v1/entries",
        headers=auth_headers("worker"),
        json=system_body | {"patient_id": str(second_patient_id)},
    )
    assert cross_patient.status_code == 403

    # The same invariant is a composite FK, not only an API check.
    owner_session.add(
        Entry(
            clinic_id=clinic_id,
            patient_id=second_patient_id,
            section="system",
            origin="system",
            source_job_id=demo_id("job-worker-demo"),
        )
    )
    with pytest.raises(IntegrityError):
        owner_session.commit()
