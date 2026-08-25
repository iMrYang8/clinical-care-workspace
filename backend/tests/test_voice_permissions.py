import pytest
from fastapi.testclient import TestClient

from app.core.config import settings


def _patient(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]


def test_capture_roles_and_patient_safe_session_dto(
    client: TestClient, auth_headers
) -> None:
    patient_headers = auth_headers("patient")
    patient_id = _patient(client, patient_headers)

    forbidden = client.post(
        "/api/v1/voice/sessions",
        headers=patient_headers,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "clinic_id": "00000000-0000-0000-0000-000000000000",
            "author_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert forbidden.status_code == 403

    created = client.post(
        "/api/v1/voice/sessions",
        headers=patient_headers,
        json={
            "patient_id": patient_id,
            "capture_kind": "patient",
            "clinic_id": "00000000-0000-0000-0000-000000000000",
            "author_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert "clinic_id" not in body
    assert "raw_transcript" not in body
    assert "facts" not in body

    raw = client.get(
        f"/api/v1/voice/sessions/{body['id']}/transcript", headers=patient_headers
    )
    assert raw.status_code == 403

    own_audio = client.get(
        f"/api/v1/voice/sessions/{body['id']}/audio", headers=patient_headers
    )
    assert own_audio.status_code == 403

    publish = client.post(
        f"/api/v1/voice/sessions/{body['id']}/publish", headers=patient_headers
    )
    assert publish.status_code == 403

    clinician = auth_headers("clinician")
    clinical = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={"patient_id": patient_id, "capture_kind": "clinical"},
    )
    assert clinical.status_code == 201
    clinical_audio = client.get(
        f"/api/v1/voice/sessions/{clinical.json()['id']}/audio",
        headers=patient_headers,
    )
    assert clinical_audio.status_code == 403


def test_cross_clinic_voice_session_is_hidden(client: TestClient, auth_headers) -> None:
    clinician = auth_headers("clinician")
    patient_id = _patient(client, clinician)
    created = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={"patient_id": patient_id, "capture_kind": "clinical"},
    )
    assert created.status_code == 201

    hidden = client.get(
        f"/api/v1/voice/sessions/{created.json()['id']}",
        headers=auth_headers("other_staff"),
    )
    assert hidden.status_code == 404


def test_live_capability_never_claims_an_unimplemented_transport(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinician = auth_headers("clinician")
    session_id = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={
            "patient_id": _patient(client, clinician),
            "capture_kind": "clinical",
        },
    ).json()["id"]
    disabled = client.get(
        f"/api/v1/voice/sessions/{session_id}/live", headers=clinician
    )
    assert disabled.json() == {
        "available": False,
        "status": "unavailable",
        "reason_code": "LIVE_TRANSCRIPT_NOT_CONFIGURED",
    }
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_ENABLED", True)
    gated = client.get(f"/api/v1/voice/sessions/{session_id}/live", headers=clinician)
    assert gated.json() == {
        "available": False,
        "status": "unavailable",
        "reason_code": "LIVE_TRANSCRIPT_TRANSPORT_UNAVAILABLE",
    }
