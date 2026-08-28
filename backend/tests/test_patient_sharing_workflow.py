import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from app.models import (
    AuditEvent,
    ConflictCase,
    Entry,
    PatientPublication,
    PatientSharingRequest,
)


def _linked_patient_id(client: TestClient, auth_headers) -> str:
    response = client.get("/api/v1/patients", headers=auth_headers("patient"))
    assert response.status_code == 200, response.text
    return response.json()["data"][0]["id"]


def test_staff_request_clinician_publish_and_withdrawal_receipt(
    client: TestClient,
    auth_headers,
    owner_session: Session,
) -> None:
    patient_headers = auth_headers("patient")
    staff_headers = auth_headers("staff")
    clinician_headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=patient_headers).json()["data"][
        0
    ]["id"]

    created = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Home hydration update",
            "content": "Patient reports tolerating the clinician-approved fluid plan.",
            "patient_facing": False,
        },
    )
    assert created.status_code == 201, created.text
    entry = created.json()

    direct_publish = client.post(
        f"/api/v1/entries/{entry['id']}/patient-publications",
        headers=clinician_headers,
        json={"entry_version_id": entry["version_id"]},
    )
    assert direct_publish.status_code == 409, direct_publish.text
    assert direct_publish.json()["detail"]["code"] == "STAFF_SHARING_REQUEST_REQUIRED"
    still_internal = client.get(
        f"/api/v1/entries/{entry['id']}", headers=clinician_headers
    )
    assert still_internal.status_code == 200
    assert still_internal.json()["patient_facing"] is False

    requested = client.post(
        f"/api/v1/entries/{entry['id']}/patient-sharing-requests",
        headers=staff_headers,
        json={"entry_version_id": entry["version_id"]},
    )
    assert requested.status_code == 201, requested.text
    request = requested.json()
    assert request["status"] == "pending"
    assert request["entry_title"] == "Home hydration update"

    staff_cannot_approve = client.post(
        f"/api/v1/patient-sharing-requests/{request['id']}/approve",
        headers=staff_headers,
    )
    assert staff_cannot_approve.status_code == 403

    queue = client.get(
        f"/api/v1/patients/{patient_id}/patient-sharing-requests",
        headers=clinician_headers,
    )
    assert queue.status_code == 200, queue.text
    assert queue.json()[0]["requested_by_name"]
    assert queue.json()[0]["status"] == "pending"

    published = client.post(
        f"/api/v1/patient-sharing-requests/{request['id']}/approve",
        headers=clinician_headers,
    )
    assert published.status_code == 201, published.text
    publication = published.json()
    assert publication["entry_id"] == entry["id"]
    assert publication["entry_title"] == "Home hydration update"
    assert publication["withdrawn_at"] is None
    assert publication["items"]

    replayed = client.post(
        f"/api/v1/patient-sharing-requests/{request['id']}/approve",
        headers=clinician_headers,
    )
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["id"] == publication["id"]

    owner_session.expire_all()
    stored_request = owner_session.get(PatientSharingRequest, request["id"])
    assert stored_request is not None
    assert stored_request.publication_id == uuid.UUID(publication["id"])
    active_publications = owner_session.exec(
        select(PatientPublication).where(
            PatientPublication.entry_id == entry["id"],
            col(PatientPublication.withdrawn_at).is_(None),
        )
    ).all()
    assert [item.id for item in active_publications] == [uuid.UUID(publication["id"])]

    patient_timeline = client.get(
        f"/api/v1/patients/{patient_id}/timeline", headers=patient_headers
    )
    assert patient_timeline.status_code == 200, patient_timeline.text
    shared = next(
        item for item in patient_timeline.json()["data"] if item["id"] == entry["id"]
    )
    assert shared["approval_receipt"]["approved_by"]
    assert shared["approval_receipt"]["withdrawal_status"] == "active"

    withdrawn = client.post(
        f"/api/v1/patient-publications/{publication['id']}/withdraw",
        headers=clinician_headers,
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["withdrawn_at"] is not None

    patient_timeline = client.get(
        f"/api/v1/patients/{patient_id}/timeline", headers=patient_headers
    )
    assert patient_timeline.status_code == 200, patient_timeline.text
    assert entry["id"] not in {item["id"] for item in patient_timeline.json()["data"]}
    direct_read = client.get(f"/api/v1/entries/{entry['id']}", headers=patient_headers)
    assert direct_read.status_code == 404

    receipts = client.get(
        f"/api/v1/patients/{patient_id}/publication-receipts",
        headers=patient_headers,
    )
    assert receipts.status_code == 200, receipts.text
    receipt = next(
        item
        for item in receipts.json()
        if item["entry_title"] == "Home hydration update"
    )
    assert receipt["status"] == "withdrawn"
    assert receipt["withdrawn_at"] is not None

    owner_session.expire_all()
    stored = owner_session.exec(select(PatientPublication)).one()
    assert stored.withdrawn_at is not None
    actions = set(owner_session.exec(select(AuditEvent.action)).all())
    assert "entry.patient_sharing_requested" in actions
    assert "entry.patient_sharing_approved" in actions
    assert "patient_publication.withdrawn" in actions


def test_clinician_patient_facing_create_respects_existing_high_risk_conflict(
    client: TestClient,
    auth_headers,
    owner_session: Session,
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_id = _linked_patient_id(client, auth_headers)

    first = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Earlier plan",
            "content": "Earlier internal plan.",
            "patient_facing": False,
        },
    )
    second = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Conflicting plan",
            "content": "Conflicting internal plan.",
            "patient_facing": False,
        },
    )
    assert first.status_code == second.status_code == 201
    first_entry = owner_session.get(Entry, first.json()["id"])
    assert first_entry is not None
    owner_session.add(
        ConflictCase(
            clinic_id=first_entry.clinic_id,
            patient_id=first_entry.patient_id,
            left_entry_id=first.json()["id"],
            right_entry_id=second.json()["id"],
            fact_type="medication",
            normalized_key="insulin",
            severity="critical",
            status="unresolved",
        )
    )
    owner_session.commit()

    blocked = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Unsafe patient update",
            "content": "This should remain internal while the conflict is open.",
            "patient_facing": True,
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "UNRESOLVED_CLINICAL_CONFLICT"
    assert owner_session.exec(select(PatientPublication)).all() == []


def test_new_version_supersedes_pending_request_and_active_publication(
    client: TestClient,
    auth_headers,
    owner_session: Session,
) -> None:
    staff_headers = auth_headers("staff")
    clinician_headers = auth_headers("clinician")
    patient_id = _linked_patient_id(client, auth_headers)
    created = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Care plan",
            "content": "Initial reviewed care plan.",
            "patient_facing": False,
        },
    ).json()
    first_request = client.post(
        f"/api/v1/entries/{created['id']}/patient-sharing-requests",
        headers=staff_headers,
        json={"entry_version_id": created["version_id"]},
    ).json()

    edited = client.patch(
        f"/api/v1/entries/{created['id']}",
        headers=staff_headers | {"If-Match": created["version_id"]},
        json={"content": "Updated reviewed care plan."},
    )
    assert edited.status_code == 200, edited.text
    owner_session.expire_all()
    stale = owner_session.get(PatientSharingRequest, first_request["id"])
    assert stale is not None
    assert stale.status == "superseded"

    second_request = client.post(
        f"/api/v1/entries/{created['id']}/patient-sharing-requests",
        headers=staff_headers,
        json={"entry_version_id": edited.json()["version_id"]},
    ).json()
    first_publication = client.post(
        f"/api/v1/patient-sharing-requests/{second_request['id']}/approve",
        headers=clinician_headers,
    )
    assert first_publication.status_code == 201, first_publication.text

    published_entry = client.get(
        f"/api/v1/entries/{created['id']}", headers=clinician_headers
    ).json()
    revised = client.patch(
        f"/api/v1/entries/{created['id']}",
        headers=staff_headers | {"If-Match": published_entry["version_id"]},
        json={"content": "Latest plan after follow-up.", "patient_facing": True},
    )
    assert revised.status_code == 200, revised.text
    queue = client.get(
        f"/api/v1/patients/{patient_id}/patient-sharing-requests",
        headers=clinician_headers,
    ).json()
    current_request = next(item for item in queue if item["status"] == "pending")
    second_publication = client.post(
        f"/api/v1/patient-sharing-requests/{current_request['id']}/approve",
        headers=clinician_headers,
    )
    assert second_publication.status_code == 201, second_publication.text
    assert (
        second_publication.json()["supersedes_publication_id"]
        == first_publication.json()["id"]
    )

    owner_session.expire_all()
    publications = owner_session.exec(
        select(PatientPublication).where(PatientPublication.entry_id == created["id"])
    ).all()
    assert len(publications) == 2
    assert sum(item.withdrawn_at is None for item in publications) == 1
    linked = owner_session.get(PatientSharingRequest, current_request["id"])
    assert linked is not None
    assert linked.publication_id == uuid.UUID(second_publication.json()["id"])
