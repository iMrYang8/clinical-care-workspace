import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import Entry, EntryRelation, EntryVersion
from app.seed import demo_id, seed_demo_data
from app.services.nightingale import decrypt_version


def test_timeline_exposes_system_author_and_direct_ai_source(
    client: TestClient,
    auth_headers,
    owner_session: Session,
) -> None:
    seed_demo_data(owner_session)
    response = client.get(
        f"/api/v1/patients/{demo_id('patient-primary')}/timeline",
        headers=auth_headers("clinician"),
    )
    assert response.status_code == 200, response.text
    rows = response.json()["data"]

    ai_rows = [row for row in rows if row["entry_type"].startswith("ai_")]
    assert {row["entry_type"] for row in ai_rows} >= {
        "ai_doctor_consult_summary",
        "ai_nurse_consult_summary",
        "ai_patient_session_summary",
    }
    for row in ai_rows:
        assert row["author_role"] == "system"
        provenance = row["provenance"]
        assert provenance["status"] == "resolved"
        assert provenance["source_entry_id"]
        assert provenance["source_entry_version_id"]
        assert provenance["exact_quote"]

        source_version = owner_session.get(
            EntryVersion, uuid.UUID(provenance["source_entry_version_id"])
        )
        source_entry = owner_session.get(
            Entry, uuid.UUID(provenance["source_entry_id"])
        )
        assert source_version is not None
        assert source_entry is not None
        assert source_version.entry_id == source_entry.id
        assert source_entry.clinic_id == demo_id("clinic-primary")
        assert source_entry.patient_id == demo_id("patient-primary")
        assert decrypt_version(source_version)[1] == provenance["exact_quote"]

    human_roles = {
        row["entry_type"]: row["author_role"]
        for row in rows
        if row["entry_type"] in {"manual_staff_note", "manual_clinician_note"}
    }
    assert human_roles["manual_staff_note"] == "staff"
    assert human_roles["manual_clinician_note"] == "clinician"


def test_ai_entry_without_persisted_source_reports_unavailable_provenance(
    client: TestClient,
    auth_headers,
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician_headers).json()[
        "data"
    ][0]["id"]
    created = client.post(
        "/api/v1/entries",
        headers=auth_headers("worker"),
        json={
            "patient_id": patient_id,
            "section": "system",
            "origin": "ai",
            "entry_type": "ai_doctor_consult_summary",
            "title": "Unbound draft",
            "content": "A generated draft without a persisted source run.",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["author_role"] == "system"
    assert created.json()["provenance"] == {
        "source_entry_id": None,
        "source_entry_version_id": None,
        "exact_quote": None,
        "status": "unavailable",
    }

    timeline = client.get(
        f"/api/v1/patients/{patient_id}/timeline", headers=clinician_headers
    )
    assert timeline.status_code == 200, timeline.text
    row = next(
        item for item in timeline.json()["data"] if item["id"] == created.json()["id"]
    )
    assert row["author_role"] == "system"
    assert row["provenance"]["status"] == "unavailable"


def test_patient_timeline_never_exposes_ai_source_metadata(
    client: TestClient, auth_headers
) -> None:
    patient_headers = auth_headers("patient")
    patient_id = client.get("/api/v1/patients", headers=patient_headers).json()["data"][
        0
    ]["id"]
    secret_source_wording = "Internal AI source wording must stay clinical."
    ai_entry = client.post(
        "/api/v1/entries",
        headers=auth_headers("worker"),
        json={
            "patient_id": patient_id,
            "section": "system",
            "origin": "ai",
            "entry_type": "ai_patient_session_summary",
            "title": "Internal AI draft",
            "content": secret_source_wording,
        },
    )
    assert ai_entry.status_code == 201, ai_entry.text

    response = client.get(
        f"/api/v1/patients/{patient_id}/timeline", headers=patient_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert ai_entry.json()["id"] not in {row["id"] for row in body["data"]}
    assert secret_source_wording not in response.text
    assert all(row["author_role"] != "system" for row in body["data"])
    assert all(row["provenance"] is None for row in body["data"])


def test_reviewed_voice_entry_resolves_explicit_transcript_relation(
    client: TestClient,
    auth_headers,
    owner_session: Session,
) -> None:
    clinician_headers = auth_headers("clinician")
    worker_headers = auth_headers("worker")
    patient_id = client.get("/api/v1/patients", headers=clinician_headers).json()[
        "data"
    ][0]["id"]
    transcript = client.post(
        "/api/v1/entries",
        headers=worker_headers,
        json={
            "patient_id": patient_id,
            "section": "system",
            "origin": "system",
            "entry_type": "system_record",
            "title": "Visit transcript",
            "content": "Exact reviewed transcript source.",
        },
    )
    assert transcript.status_code == 201, transcript.text
    reviewed = client.post(
        "/api/v1/entries",
        headers=worker_headers,
        json={
            "patient_id": patient_id,
            "section": "system",
            "origin": "ai",
            "entry_type": "ai_doctor_consult_summary",
            "title": "Reviewed voice result",
            "content": "Generated review of the visit.",
        },
    )
    assert reviewed.status_code == 201, reviewed.text
    owner_session.add(
        EntryRelation(
            clinic_id=demo_id("clinic-primary"),
            source_entry_id=uuid.UUID(reviewed.json()["id"]),
            target_entry_id=uuid.UUID(transcript.json()["id"]),
            relation_type="derived_from_voice_transcript",
            created_by_id=demo_id("user-worker"),
        )
    )
    owner_session.commit()

    timeline = client.get(
        f"/api/v1/patients/{patient_id}/timeline", headers=clinician_headers
    )
    assert timeline.status_code == 200, timeline.text
    row = next(
        item for item in timeline.json()["data"] if item["id"] == reviewed.json()["id"]
    )
    assert row["author_role"] == "system"
    assert row["provenance"] == {
        "source_entry_id": transcript.json()["id"],
        "source_entry_version_id": transcript.json()["version_id"],
        "exact_quote": "Exact reviewed transcript source.",
        "status": "resolved",
    }
