import hashlib

from fastapi.testclient import TestClient


def _patient_id(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/v1/patients", headers=headers)
    assert response.status_code == 200
    return response.json()["data"][0]["id"]


def _session(client: TestClient, headers: dict[str, str], patient_id: str) -> str:
    response = client.post(
        "/api/v1/voice/sessions",
        headers=headers,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": True,
            "fixture_id": "code-switch-overlap-v1",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _join(
    client: TestClient, headers: dict[str, str], session_id: str, client_id: str
) -> str:
    response = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=headers,
        json={"client_device_id": client_id, "capture_role": "clinician"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _put(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    device_id: str,
    index: int,
    payload: bytes,
):
    digest = hashlib.sha256(payload).hexdigest()
    return client.put(
        f"/api/v1/voice/sessions/{session_id}/devices/{device_id}/chunks/{index}",
        headers=headers
        | {
            "Content-Type": "audio/webm",
            "X-Chunk-SHA256": digest,
            "X-Chunk-Start-Ms": str(index * 2_000),
            "X-Chunk-End-Ms": str((index + 1) * 2_000),
        },
        content=payload,
    )


def test_chunk_upload_is_idempotent_and_tamper_evident(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    session_id = _session(client, headers, _patient_id(client, headers))
    device_id = _join(client, headers, session_id, "desktop-a")

    first = _put(client, headers, session_id, device_id, 0, b"synthetic-audio-0")
    assert first.status_code == 200, first.text
    assert first.json() == {"chunk_index": 0, "acknowledged": True, "duplicate": False}

    replay = _put(client, headers, session_id, device_id, 0, b"synthetic-audio-0")
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True

    tamper = _put(client, headers, session_id, device_id, 0, b"different-bytes")
    assert tamper.status_code == 409
    assert tamper.json()["detail"]["code"] == "AUDIO_CHUNK_HASH_CONFLICT"


def test_finalize_reports_per_device_missing_chunks_and_accepts_out_of_order(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    session_id = _session(client, headers, _patient_id(client, headers))
    first_device = _join(client, headers, session_id, "desktop-a")
    second_device = _join(client, headers, session_id, "phone-b")

    assert _put(client, headers, session_id, first_device, 2, b"a2").status_code == 200
    assert _put(client, headers, session_id, first_device, 0, b"a0").status_code == 200
    assert _put(client, headers, session_id, second_device, 0, b"b0").status_code == 200

    incomplete = client.post(
        f"/api/v1/voice/sessions/{session_id}/finalize",
        headers=headers | {"Idempotency-Key": "missing-chunks-v1"},
        json={
            "devices": [
                {"device_id": first_device, "last_chunk_index": 2},
                {"device_id": second_device, "last_chunk_index": 1},
            ]
        },
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"] == {
        "code": "MISSING_AUDIO_CHUNKS",
        "missing": {
            first_device: [1],
            second_device: [1],
        },
    }

    assert _put(client, headers, session_id, first_device, 1, b"a1").status_code == 200
    assert _put(client, headers, session_id, second_device, 1, b"b1").status_code == 200
    completed = client.post(
        f"/api/v1/voice/sessions/{session_id}/finalize",
        headers=headers | {"Idempotency-Key": "missing-chunks-v1"},
        json={
            "devices": [
                {"device_id": first_device, "last_chunk_index": 2},
                {"device_id": second_device, "last_chunk_index": 1},
            ]
        },
    )
    assert completed.status_code == 202, completed.text
    assert completed.json()["job_id"]

    status = client.get(
        f"/api/v1/voice/sessions/{session_id}/chunks/status", headers=headers
    )
    assert status.status_code == 200
    assert status.json()["uploaded_chunks"] == 5
    assert status.json()["devices"][0]["received_indices"] == [0, 1, 2]
