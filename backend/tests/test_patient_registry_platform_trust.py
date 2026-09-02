from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    ConflictCase,
    NotificationOutbox,
    PatientIdentifier,
    PatientUserLink,
    PlatformAuditEvent,
)


def _identity(
    *, name: str = "Morgan Lim", mrn: str = "MRN-UAT-001", document: str = "S9999999Z"
) -> dict[str, str]:
    return {
        "display_name": name,
        "date_of_birth": "1990-02-20",
        "medical_record_number": mrn,
        "identity_document_type": "nric_fin",
        "identity_document_number": document,
    }


def test_staff_creates_encrypted_patient_and_exact_duplicate_is_blocked(
    client: TestClient, auth_headers, owner_session
) -> None:
    staff = auth_headers("staff")
    clear = client.post(
        "/api/v1/patients/duplicate-check", headers=staff, json=_identity()
    )
    assert clear.status_code == 200
    assert clear.json()["status"] == "clear"
    created = client.post(
        "/api/v1/patients",
        headers=staff | {"Idempotency-Key": "patient-registry-test"},
        json=_identity(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["masked_identity_document"] == "••••999Z"
    replay = client.post(
        "/api/v1/patients",
        headers=staff | {"Idempotency-Key": "patient-registry-test"},
        json=_identity(),
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]
    exact = client.post(
        "/api/v1/patients/duplicate-check", headers=staff, json=_identity()
    )
    assert exact.json()["status"] == "exact_match"
    identifiers = owner_session.exec(
        select(PatientIdentifier).where(
            PatientIdentifier.patient_id == created.json()["id"]
        )
    ).all()
    assert len(identifiers) == 2
    assert all(
        item.value_ciphertext not in {b"MRNUAT001", b"S9999999Z"}
        for item in identifiers
    )
    assert all(len(item.value_hmac) == 64 for item in identifiers)
    forbidden = client.post(
        "/api/v1/patients",
        headers=auth_headers("admin"),
        json=_identity(mrn="MRN2", document="S2X"),
    )
    assert forbidden.status_code == 403


def test_possible_duplicate_requires_bound_confirmation_token(
    client: TestClient, auth_headers
) -> None:
    staff = auth_headers("staff")
    first = client.post("/api/v1/patients", headers=staff, json=_identity())
    assert first.status_code == 201, first.text
    second = _identity(mrn="MRN-UAT-002", document="S8888888A")
    possible = client.post(
        "/api/v1/patients/duplicate-check", headers=staff, json=second
    )
    assert possible.status_code == 200
    assert possible.json()["status"] == "possible_match"
    blocked = client.post("/api/v1/patients", headers=staff, json=second)
    assert blocked.status_code == 409
    confirmed = client.post(
        "/api/v1/patients",
        headers=staff,
        json=second
        | {
            "duplicate_confirmation_token": possible.json()[
                "duplicate_confirmation_token"
            ]
        },
    )
    assert confirmed.status_code == 201, confirmed.text


def test_patient_invitation_creates_membership_and_patient_link(
    client: TestClient, auth_headers: Any, owner_session: Session
) -> None:
    staff = auth_headers("staff")
    patient = client.post("/api/v1/patients", headers=staff, json=_identity()).json()
    invited = client.post(
        f"/api/v1/patients/{patient['id']}/portal-invitations",
        headers=staff,
        json={"email": "new.patient@example.com"},
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["notification_state"] == "submitted"
    owner_session.expire_all()
    notification = owner_session.get(
        NotificationOutbox, uuid.UUID(invited.json()["notification_id"])
    )
    assert notification is not None
    delivered = field_codec.decrypt_json(
        notification.clinic_id,
        "notification.payload",
        notification.id,
        notification.payload_ciphertext,
    )
    recipient = field_codec.decrypt_text(
        notification.clinic_id,
        "notification.destination",
        notification.id,
        notification.destination_ciphertext,
    )
    assert isinstance(delivered, dict)
    assert recipient == "new.patient@example.com"
    token = delivered["enrollment_token"]
    assert isinstance(token, str)
    preview = client.post(
        "/api/v1/auth/patient-invitations/preview",
        json={"token": token, "email": recipient},
    )
    assert preview.status_code == 200
    assert preview.json()["account_exists"] is False
    accepted = client.post(
        "/api/v1/auth/patient-invitations/accept",
        json={
            "token": token,
            "email": recipient,
            "password": "patient-portal-passphrase",
            "full_name": "Morgan Lim",
        },
    )
    assert accepted.status_code == 200, accepted.text
    links = owner_session.exec(
        select(PatientUserLink).where(PatientUserLink.patient_id == patient["id"])
    ).all()
    assert len(links) == 1
    replay = client.post(
        "/api/v1/auth/patient-invitations/accept",
        json={
            "token": token,
            "email": recipient,
            "password": "patient-portal-passphrase",
        },
    )
    assert replay.status_code == 400


def test_platform_administrator_is_separate_read_only_and_audited(
    client: TestClient, owner_session
) -> None:
    logged_in = client.post(
        "/api/v1/platform/auth/login",
        json={
            "email": "platform.admin@nightingale.example",
            "password": "local-platform-owner-only",
        },
    )
    assert logged_in.status_code == 200, logged_in.text
    client.cookies.set(
        settings.PLATFORM_AUTH_COOKIE_NAME, logged_in.json()["access_token"]
    )
    clinics = client.get("/api/v1/platform/clinics")
    assert clinics.status_code == 200, clinics.text
    assert {item["code"] for item in clinics.json()["data"]} == {
        "NIGHTINGALE",
        "OTHERCLINIC",
    }
    patients = client.get("/api/v1/platform/clinics/NIGHTINGALE/patients")
    assert patients.status_code == 200, patients.text
    assert patients.json()
    assert all("identity_document_number" not in item for item in patients.json())
    assert owner_session.exec(select(PlatformAuditEvent)).all()
    ordinary_write = client.post("/api/v1/entries", json={})
    assert ordinary_write.status_code in {401, 403, 422}


def test_human_human_allergy_conflict_requires_clinician_correction(
    client: TestClient, auth_headers, owner_session
) -> None:
    staff = auth_headers("staff")
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=staff).json()["data"][0]["id"]
    left = client.post(
        "/api/v1/entries",
        headers=staff,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Allergy intake",
            "content": "Patient is allergic to penicillin.",
        },
    )
    assert left.status_code == 201, left.text
    right = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Medication review",
            "content": "No allergy to penicillin was reported.",
        },
    )
    assert right.status_code == 201, right.text
    conflicts = client.get(
        f"/api/v1/patients/{patient_id}/conflicts", headers=clinician
    )
    assert conflicts.status_code == 200
    conflict = conflicts.json()[0]
    assert conflict["severity"] == "critical"
    assert conflict["left_pointer_id"] and conflict["right_pointer_id"]
    correction = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Verified allergy correction",
            "content": "Identity and source documents reviewed by clinician.",
        },
    )
    resolved = client.post(
        f"/api/v1/conflicts/{conflict['id']}/resolve",
        headers=clinician,
        json={
            "resolution": "Verified against the original allergy record.",
            "correction_entry_id": correction.json()["id"],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert (
        owner_session.exec(
            select(ConflictCase).where(ConflictCase.id == conflict["id"])
        )
        .one()
        .resolved_by_membership_id
    )
