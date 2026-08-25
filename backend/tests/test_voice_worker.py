import asyncio
import hashlib
import io
import math
import struct
import uuid
import wave

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.models import (
    AudioAsset,
    Job,
    ProvenancePointer,
    TranscriptRevision,
    VoiceSession,
)
from app.services.ai_jobs import worker_context_for_job
from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
    validate_transcript_result,
)
from app.services.voice.providers.deterministic import SyntheticFixtureProvider
from app.services.voice.worker import process_voice_job


@pytest.mark.unit
def test_fixture_provider_is_explicit_and_normalized() -> None:
    provider = SyntheticFixtureProvider()
    result = provider.transcribe_fixture("code-switch-overlap-v1")
    assert result.provider == "deterministic-synthetic-fixture"
    assert result.model == "code-switch-overlap-v1"
    assert [segment.speaker_id for segment in result.segments] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert {segment.detected_language for segment in result.segments} == {"en", "zh"}
    assert result.segments[1].overlap_group_id == "overlap-1"
    validate_transcript_result(result)

    with pytest.raises(ValueError, match="Unknown synthetic fixture"):
        provider.transcribe_fixture("not-a-fixture")


@pytest.mark.unit
def test_invalid_transcript_segments_are_rejected() -> None:
    with pytest.raises(ValueError, match="segment time range"):
        validate_transcript_result(
            TranscriptResult(
                text="bad",
                segments=[
                    TranscriptSegmentResult(
                        text="bad",
                        start_ms=100,
                        end_ms=10,
                        speaker_id=None,
                        detected_language="en",
                        confidence=None,
                        confidence_source="unavailable",
                        overlap_group_id=None,
                    )
                ],
                provider="test",
                model="test",
            )
        )


@pytest.mark.unit
def test_result_contract_rejects_noncontiguous_text_offsets() -> None:
    result = TranscriptResult(
        text="hello world",
        segments=[
            TranscriptSegmentResult(
                text="hello",
                start_ms=0,
                end_ms=500,
                speaker_id=None,
                detected_language="en",
                confidence=0.9,
                confidence_source="provider",
                overlap_group_id=None,
                text_start=4,
                text_end=9,
            )
        ],
        provider="test",
        model=str(uuid.uuid4()),
    )
    with pytest.raises(ValueError, match="text span"):
        validate_transcript_result(result)


def _wav_fixture(duration_seconds: float = 11.0) -> bytes:
    sample_rate = 16_000
    frame_count = int(duration_seconds * sample_rate)
    frames = bytearray()
    for index in range(frame_count):
        sample = int(4_000 * math.sin(2 * math.pi * 220 * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(frames)
    return output.getvalue()


def _create_recording(
    client: TestClient,
    headers: dict[str, str],
    *,
    synthetic_fixture: bool,
) -> tuple[str, str, str]:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    created = client.post(
        "/api/v1/voice/sessions",
        headers=headers,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": synthetic_fixture,
            "fixture_id": "code-switch-overlap-v1" if synthetic_fixture else None,
        },
    )
    assert created.status_code == 201, created.text
    voice_session_id = created.json()["id"]
    joined = client.post(
        f"/api/v1/voice/sessions/{voice_session_id}/devices",
        headers=headers,
        json={"client_device_id": "worker-fixture", "capture_role": "patient"},
    )
    assert joined.status_code == 201, joined.text
    # capture_role is derived from the trusted membership, never the body.
    assert joined.json()["capture_role"] == "clinician"
    payload = _wav_fixture()
    uploaded = client.put(
        f"/api/v1/voice/sessions/{voice_session_id}/devices/{joined.json()['id']}/chunks/0",
        headers=headers
        | {
            "Content-Type": "audio/wav",
            "X-Chunk-SHA256": hashlib.sha256(payload).hexdigest(),
            "X-Chunk-Start-Ms": "0",
            "X-Chunk-End-Ms": "11000",
        },
        content=payload,
    )
    assert uploaded.status_code == 200, uploaded.text
    finalized = client.post(
        f"/api/v1/voice/sessions/{voice_session_id}/finalize",
        headers=headers | {"Idempotency-Key": f"voice-{voice_session_id}"},
        json={"devices": [{"device_id": joined.json()["id"], "last_chunk_index": 0}]},
    )
    assert finalized.status_code == 202, finalized.text
    return patient_id, voice_session_id, finalized.json()["job_id"]


def _run_job(job_id: str) -> None:
    with Session(engine) as db:
        job = db.exec(select(Job).where(Job.id == uuid.UUID(job_id))).one()
        context = worker_context_for_job(db, job)
        assert context is not None
        asyncio.run(process_voice_job(db, context, job.id))


def test_synthetic_worker_persists_normalized_review_and_is_idempotent(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    _run_job(job_id)

    status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "needs_review"
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=headers
    )
    assert transcript.status_code == 200, transcript.text
    body = transcript.json()
    assert body["provider"] == "deterministic-synthetic-fixture"
    assert [item["speaker_id"] for item in body["segments"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert {item["detected_language"] for item in body["segments"]} == {"en", "zh"}
    assert body["segments"][1]["overlap_group_id"] == "overlap-1"
    assert body["facts"][0]["exact_quote"] == "penicillin allergy"

    with Session(engine) as db:
        assets = db.exec(select(AudioAsset)).all()
        revisions = db.exec(select(TranscriptRevision)).all()
        assert len(assets) == 1
        assert len(revisions) == 1
        assert assets[0].payload_ciphertext[:1] == b"\x01"

    # The durable job cannot be claimed again and derived rows remain unique.
    with pytest.raises(HTTPException):
        _run_job(job_id)
    with Session(engine) as db:
        assert len(db.exec(select(AudioAsset)).all()) == 1
        assert len(db.exec(select(TranscriptRevision)).all()) == 1


def test_provider_disabled_retains_encrypted_audio_without_fake_transcript(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "disabled")
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=False
    )
    _run_job(job_id)

    status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "needs_review"
    assert status.json()["error_code"] == "ASR_PROVIDER_DISABLED"
    assert (
        client.get(
            f"/api/v1/voice/sessions/{session_id}/transcript", headers=headers
        ).status_code
        == 404
    )
    with Session(engine) as db:
        assert len(db.exec(select(AudioAsset)).all()) == 1
        assert len(db.exec(select(TranscriptRevision)).all()) == 0


def test_correction_marks_stale_reanalysis_restores_provenance_and_publish(
    client: TestClient, auth_headers
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(job_id)
    corrected = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={"text": "Patient confirms a penicillin allergy during review."},
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["stale"] is True
    assert "DOWNSTREAM_RESULTS_STALE" in corrected.json()["warning_codes"]
    blocked = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish", headers=clinician
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "DOWNSTREAM_RESULTS_STALE"

    reanalyze = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "corrected-reanalysis-v1"},
    )
    assert reanalyze.status_code == 202, reanalyze.text
    _run_job(reanalyze.json()["job_id"])
    reviewed = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["stale"] is False
    assert reviewed.json()["previous_revision_id"] == corrected.json()["id"]
    assert reviewed.json()["facts"]

    published = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish", headers=clinician
    )
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
    with Session(engine) as db:
        pointer = db.exec(select(ProvenancePointer)).one()
        voice_session = db.exec(
            select(VoiceSession).where(VoiceSession.id == uuid.UUID(session_id))
        ).one()
        assert pointer.clinical_fact_id is not None
        assert pointer.audio_asset_id is not None
        assert pointer.audio_start_ms == 0
        assert pointer.audio_end_ms is not None
        assert voice_session.published_entry_id == uuid.UUID(
            published.json()["entry_id"]
        )

    patient = auth_headers("patient")
    patient_status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=patient)
    assert patient_status.status_code == 200
    assert patient_status.json()["patient_summary"]
    assert patient_status.json()["current_transcript_revision_id"] is None
    assert "penicillin allergy during review" not in patient_status.text
