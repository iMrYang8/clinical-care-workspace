import asyncio
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.routes import voice as voice_routes
from app.core.config import settings
from app.models import AudioChunkAck


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
    session_status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=headers)
    assert session_status.status_code == 200
    response = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=headers,
        json={
            "client_device_id": client_id,
            "capture_role": "clinician",
            "expected_patient_id": session_status.json()["patient_id"],
            "expected_capture_kind": session_status.json()["capture_kind"],
        },
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


def _seal(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    device_id: str,
    last_chunk_index: int,
):
    return client.post(
        f"/api/v1/voice/sessions/{session_id}/devices/{device_id}/seal",
        headers=headers,
        json={"last_chunk_index": last_chunk_index},
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

    metadata_tamper = client.put(
        f"/api/v1/voice/sessions/{session_id}/devices/{device_id}/chunks/0",
        headers=headers
        | {
            "Content-Type": "audio/webm",
            "X-Chunk-SHA256": hashlib.sha256(b"synthetic-audio-0").hexdigest(),
            "X-Chunk-Start-Ms": "100",
            "X-Chunk-End-Ms": "2100",
        },
        content=b"synthetic-audio-0",
    )
    assert metadata_tamper.status_code == 409
    assert metadata_tamper.json()["detail"]["code"] == "AUDIO_CHUNK_METADATA_CONFLICT"

    tamper = _put(client, headers, session_id, device_id, 0, b"different-bytes")
    assert tamper.status_code == 409
    assert tamper.json()["detail"]["code"] == "AUDIO_CHUNK_HASH_CONFLICT"
    assert _seal(client, headers, session_id, device_id, 0).status_code == 200

    replay_after_seal = _put(
        client, headers, session_id, device_id, 0, b"synthetic-audio-0"
    )
    assert replay_after_seal.status_code == 200
    assert replay_after_seal.json()["duplicate"] is True
    metadata_tamper_after_seal = client.put(
        f"/api/v1/voice/sessions/{session_id}/devices/{device_id}/chunks/0",
        headers=headers
        | {
            "Content-Type": "audio/webm",
            "X-Chunk-SHA256": hashlib.sha256(b"synthetic-audio-0").hexdigest(),
            "X-Chunk-Start-Ms": "1",
            "X-Chunk-End-Ms": "2001",
        },
        content=b"synthetic-audio-0",
    )
    assert metadata_tamper_after_seal.status_code == 409
    assert metadata_tamper_after_seal.json()["detail"]["code"] == (
        "AUDIO_CHUNK_METADATA_CONFLICT"
    )
    tamper_after_seal = _put(
        client, headers, session_id, device_id, 0, b"changed-after-seal"
    )
    assert tamper_after_seal.status_code == 409
    assert tamper_after_seal.json()["detail"]["code"] == ("AUDIO_CHUNK_HASH_CONFLICT")
    after_seal = _put(client, headers, session_id, device_id, 1, b"too-late")
    assert after_seal.status_code == 409
    assert after_seal.json()["detail"]["code"] == "VOICE_DEVICE_SEALED"

    finalized = client.post(
        f"/api/v1/voice/sessions/{session_id}/finalize",
        headers=headers | {"Idempotency-Key": "replay-after-finalize"},
        json={"devices": [{"device_id": device_id, "last_chunk_index": 0}]},
    )
    assert finalized.status_code == 202, finalized.text
    replay_after_finalize = _put(
        client, headers, session_id, device_id, 0, b"synthetic-audio-0"
    )
    assert replay_after_finalize.status_code == 200
    assert replay_after_finalize.json()["duplicate"] is True


def test_chunk_and_session_size_limits_are_enforced_before_storage(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = auth_headers("clinician")
    session_id = _session(client, headers, _patient_id(client, headers))
    device_id = _join(client, headers, session_id, "bounded-device")
    monkeypatch.setattr(settings, "VOICE_MAX_CHUNK_BYTES", 4)
    oversized = _put(client, headers, session_id, device_id, 0, b"12345")
    assert oversized.status_code == 413
    assert oversized.json()["detail"]["code"] == "AUDIO_CHUNK_SIZE_INVALID"

    monkeypatch.setattr(settings, "VOICE_MAX_CHUNK_BYTES", 10)
    monkeypatch.setattr(settings, "VOICE_MAX_SESSION_BYTES", 8)
    assert _put(client, headers, session_id, device_id, 0, b"1234").status_code == 200
    session_limit = _put(client, headers, session_id, device_id, 1, b"56789")
    assert session_limit.status_code == 413
    assert session_limit.json()["detail"]["code"] == "VOICE_SESSION_SIZE_LIMIT_REACHED"


def test_device_seal_rejects_truncating_received_track(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    session_id = _session(client, headers, _patient_id(client, headers))
    device_id = _join(client, headers, session_id, "seal-boundary")
    assert _put(client, headers, session_id, device_id, 0, b"zero").status_code == 200
    assert _put(client, headers, session_id, device_id, 1, b"one").status_code == 200
    truncated = _seal(client, headers, session_id, device_id, 0)
    assert truncated.status_code == 409
    assert truncated.json()["detail"]["code"] == "AUDIO_CHUNKS_AFTER_DECLARED_END"


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
    premature = client.post(
        f"/api/v1/voice/sessions/{session_id}/finalize",
        headers=headers | {"Idempotency-Key": "premature-finalize-v1"},
        json={
            "devices": [
                {"device_id": first_device, "last_chunk_index": 2},
                {"device_id": second_device, "last_chunk_index": 1},
            ]
        },
    )
    assert premature.status_code == 409
    assert premature.json()["detail"]["code"] == "VOICE_DEVICES_NOT_SEALED"
    assert _seal(client, headers, session_id, first_device, 2).status_code == 200
    assert _seal(client, headers, session_id, second_device, 1).status_code == 200
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


def test_empty_crashed_device_can_be_abandoned_before_other_track_finalizes(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    patient_id = _patient_id(client, headers)
    session_id = _session(client, headers, patient_id)
    complete_device = _join(client, headers, session_id, "complete-device")
    crashed_device = _join(client, headers, session_id, "crashed-before-chunk")

    assert (
        _put(client, headers, session_id, complete_device, 0, b"complete").status_code
        == 200
    )
    assert _seal(client, headers, session_id, complete_device, 0).status_code == 200
    blocked = client.post(
        f"/api/v1/voice/sessions/{session_id}/finalize",
        headers=headers | {"Idempotency-Key": "empty-device-blocks"},
        json={
            "devices": [
                {"device_id": complete_device, "last_chunk_index": 0},
            ]
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == ("VOICE_DEVICE_DECLARATIONS_INCOMPLETE")

    other_member = client.delete(
        f"/api/v1/voice/sessions/{session_id}/devices/{crashed_device}",
        headers=auth_headers("staff"),
    )
    assert other_member.status_code == 403
    assert other_member.json()["detail"] == "Device belongs to another member"

    cannot_abandon_audio = client.delete(
        f"/api/v1/voice/sessions/{session_id}/devices/{complete_device}",
        headers=headers,
    )
    assert cannot_abandon_audio.status_code == 409
    assert cannot_abandon_audio.json()["detail"]["code"] == ("VOICE_DEVICE_HAS_AUDIO")
    abandoned = client.delete(
        f"/api/v1/voice/sessions/{session_id}/devices/{crashed_device}",
        headers=headers,
    )
    assert abandoned.status_code == 200
    assert abandoned.json() == {
        "device_id": crashed_device,
        "abandoned": True,
    }

    finalized = client.post(
        f"/api/v1/voice/sessions/{session_id}/finalize",
        headers=headers | {"Idempotency-Key": "after-empty-device-abandon"},
        json={
            "devices": [
                {"device_id": complete_device, "last_chunk_index": 0},
            ]
        },
    )
    assert finalized.status_code == 202, finalized.text


def test_session_rejects_more_devices_than_finalize_can_declare(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    patient_id = _patient_id(client, headers)
    session_id = _session(client, headers, patient_id)
    for index in range(8):
        joined = client.post(
            f"/api/v1/voice/sessions/{session_id}/devices",
            headers=headers,
            json={
                "client_device_id": f"device-{index}",
                "capture_role": "patient",
                "expected_patient_id": patient_id,
                "expected_capture_kind": "clinical",
            },
        )
        assert joined.status_code == 201
    rejected = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=headers,
        json={
            "client_device_id": "device-8",
            "capture_role": "patient",
            "expected_patient_id": patient_id,
            "expected_capture_kind": "clinical",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "VOICE_DEVICE_LIMIT_REACHED"


def test_join_rejects_wrong_patient_or_capture_context(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    patient_id = _patient_id(client, headers)
    session_id = _session(client, headers, patient_id)
    wrong_patient = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=headers,
        json={
            "client_device_id": "wrong-patient-device",
            "capture_role": "clinician",
            "expected_patient_id": str(uuid.uuid4()),
            "expected_capture_kind": "clinical",
        },
    )
    assert wrong_patient.status_code == 409
    assert wrong_patient.json()["detail"]["code"] == ("VOICE_SESSION_CONTEXT_MISMATCH")
    wrong_kind = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=headers,
        json={
            "client_device_id": "wrong-kind-device",
            "capture_role": "clinician",
            "expected_patient_id": patient_id,
            "expected_capture_kind": "patient",
        },
    )
    assert wrong_kind.status_code == 409
    assert wrong_kind.json()["detail"]["code"] == ("VOICE_SESSION_CONTEXT_MISMATCH")


@pytest.mark.unit
def test_chunk_body_is_consumed_before_session_row_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def read_body(_request: object) -> bytes:
        order.append("body-start")
        await asyncio.sleep(0)
        order.append("body-complete")
        return b"audio"

    def lock_session(*_args: object, **kwargs: object) -> object:
        assert kwargs["lock"] is True
        order.append("session-lock")
        return object()

    def store_chunk(*_args: object, **_kwargs: object) -> AudioChunkAck:
        order.append("chunk-store")
        return AudioChunkAck(chunk_index=0)

    monkeypatch.setattr(voice_routes, "_bounded_chunk_body", read_body)
    monkeypatch.setattr(voice_routes, "get_voice_session", lock_session)
    monkeypatch.setattr(voice_routes, "upload_audio_chunk", store_chunk)
    request = SimpleNamespace(headers={"content-type": "audio/webm"})
    session = SimpleNamespace(commit=lambda: order.append("commit"))
    result = asyncio.run(
        voice_routes.put_chunk(
            uuid.uuid4(),
            uuid.uuid4(),
            0,
            request,  # type: ignore[arg-type]
            session,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            "0" * 64,
            0,
            2_000,
        )
    )
    assert result.acknowledged is True
    assert order == [
        "body-start",
        "body-complete",
        "session-lock",
        "chunk-store",
        "commit",
    ]


@pytest.mark.unit
def test_chunk_body_read_has_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowRequest:
        headers: dict[str, str] = {}

        async def stream(self):
            await asyncio.sleep(0.1)
            yield b"late"

    monkeypatch.setattr(settings, "VOICE_CHUNK_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            voice_routes._bounded_chunk_body(SlowRequest())  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 408
    assert exc_info.value.detail == {"code": "AUDIO_CHUNK_READ_TIMEOUT"}
