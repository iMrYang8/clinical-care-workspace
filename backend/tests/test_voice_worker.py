import asyncio
import hashlib
import io
import math
import struct
import uuid
import wave
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.core.field_crypto import field_codec
from app.models import (
    AudioAsset,
    ClinicalFact,
    Job,
    ProvenancePointer,
    TranscriptRevision,
    VoiceSession,
)
from app.services.ai_jobs import worker_context_for_job
from app.services.voice import worker as voice_worker
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


@pytest.mark.unit
def test_result_contract_requires_explicit_overlap_labels() -> None:
    with pytest.raises(ValueError, match="chronological unless marked overlap"):
        validate_transcript_result(
            TranscriptResult(
                text="first\nsecond",
                segments=[
                    TranscriptSegmentResult(
                        text="first",
                        start_ms=0,
                        end_ms=1_000,
                        speaker_id="A",
                        detected_language="en",
                        confidence=0.9,
                        confidence_source="provider",
                        overlap_group_id=None,
                        text_start=0,
                        text_end=5,
                    ),
                    TranscriptSegmentResult(
                        text="second",
                        start_ms=900,
                        end_ms=1_500,
                        speaker_id="B",
                        detected_language="en",
                        confidence=0.9,
                        confidence_source="provider",
                        overlap_group_id=None,
                        text_start=6,
                        text_end=12,
                    ),
                ],
                provider="test",
                model="overlap-test",
            )
        )


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
    sealed = client.post(
        f"/api/v1/voice/sessions/{voice_session_id}/devices/{joined.json()['id']}/seal",
        headers=headers,
        json={"last_chunk_index": 0},
    )
    assert sealed.status_code == 200, sealed.text
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

    with Session(engine) as db:
        immutable_revision = db.exec(select(TranscriptRevision)).one()
        immutable_revision.status = "ready"
        db.add(immutable_revision)
        with pytest.raises(DBAPIError, match="append-only"):
            db.commit()

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

    async def restored_provider(
        *_args: object, **_kwargs: object
    ) -> tuple[TranscriptResult, None]:
        return (
            SyntheticFixtureProvider().transcribe_fixture("code-switch-overlap-v1"),
            None,
        )

    monkeypatch.setattr(voice_worker, "_transcribe", restored_provider)
    retried = client.post(f"/api/v1/jobs/{job_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "pending"
    _run_job(job_id)
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=headers
    )
    assert transcript.status_code == 200
    with Session(engine) as db:
        assert len(db.exec(select(AudioAsset)).all()) == 1
        assert len(db.exec(select(TranscriptRevision)).all()) == 1


def test_out_of_bounds_provider_time_never_becomes_fact_provenance(
    client: TestClient,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    text = "Safe introduction.\npenicillin allergy"

    async def out_of_bounds_provider(
        *_args: object, **_kwargs: object
    ) -> tuple[TranscriptResult, None]:
        return (
            validate_transcript_result(
                TranscriptResult(
                    text=text,
                    segments=[
                        TranscriptSegmentResult(
                            text="Safe introduction.",
                            start_ms=0,
                            end_ms=500,
                            speaker_id="SPEAKER_00",
                            detected_language="en",
                            confidence=0.99,
                            confidence_source="provider",
                            overlap_group_id=None,
                            text_start=0,
                            text_end=18,
                        ),
                        TranscriptSegmentResult(
                            text="penicillin allergy",
                            start_ms=50_000,
                            end_ms=51_000,
                            speaker_id="SPEAKER_00",
                            detected_language="en",
                            confidence=0.99,
                            confidence_source="provider",
                            overlap_group_id=None,
                            text_start=19,
                            text_end=len(text),
                        ),
                    ],
                    provider="boundary-fixture",
                    model="outside-audio-v1",
                )
            ),
            None,
        )

    monkeypatch.setattr(voice_worker, "_transcribe", out_of_bounds_provider)
    _run_job(job_id)
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=headers
    )
    assert transcript.status_code == 200
    assert "SEGMENT_TIME_OUT_OF_BOUNDS" in transcript.json()["warning_codes"]
    assert transcript.json()["facts"] == []
    assert transcript.json()["segments"][0]["end_ms"] == 500
    publish = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish", headers=headers
    )
    assert publish.status_code == 409
    assert publish.json()["detail"]["code"] == "FACT_EVIDENCE_REQUIRED"


def test_expired_worker_resumes_from_persisted_transcribing_state(
    client: TestClient,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    original_transcribe = voice_worker._transcribe

    async def crash_after_preprocessing(*_args: object, **_kwargs: object) -> None:
        raise SystemExit("simulated worker termination")

    monkeypatch.setattr(voice_worker, "_transcribe", crash_after_preprocessing)
    with pytest.raises(SystemExit, match="simulated worker termination"):
        _run_job(job_id)

    with Session(engine) as db:
        voice_session = db.exec(
            select(VoiceSession).where(VoiceSession.id == uuid.UUID(session_id))
        ).one()
        job = db.exec(select(Job).where(Job.id == uuid.UUID(job_id))).one()
        assert voice_session.state == "transcribing"
        assert len(db.exec(select(AudioAsset)).all()) == 1
        job.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        db.add(job)
        db.commit()

    monkeypatch.setattr(voice_worker, "_transcribe", original_transcribe)
    _run_job(job_id)

    with Session(engine) as db:
        voice_session = db.exec(
            select(VoiceSession).where(VoiceSession.id == uuid.UUID(session_id))
        ).one()
        assert voice_session.state == "needs_review"
        assert len(db.exec(select(AudioAsset)).all()) == 1
        assert len(db.exec(select(TranscriptRevision)).all()) == 1


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
    replay = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "corrected-reanalysis-v1"},
    )
    assert replay.status_code == 202
    assert replay.json()["job_id"] == reanalyze.json()["job_id"]
    correction_race = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={"text": "A racing correction must be rejected."},
    )
    assert correction_race.status_code == 409
    assert correction_race.json()["detail"]["code"] == "VOICE_REVIEW_STATE_CONFLICT"
    publish_race = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish", headers=clinician
    )
    assert publish_race.status_code == 409
    assert publish_race.json()["detail"]["code"] == "VOICE_NOT_PUBLISHABLE"
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
    replayed_publication = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish", headers=clinician
    )
    assert replayed_publication.status_code == 200
    assert replayed_publication.json()["entry_id"] == published.json()["entry_id"]
    published_correction = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={"text": "Published transcripts are immutable through this API."},
    )
    assert published_correction.status_code == 409
    published_reanalysis = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "after-publish"},
    )
    assert published_reanalysis.status_code == 409
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
        fact = db.get(ClinicalFact, pointer.clinical_fact_id)
        assert fact is not None
        assert fact.status == "accepted"
        assert fact.reviewed_by_id is not None
        assert fact.reviewed_at is not None

    with Session(engine) as db:
        fact = db.get(ClinicalFact, pointer.clinical_fact_id)
        assert fact is not None
        fact.value_ciphertext = b"tampered-evidence"
        db.add(fact)
        with pytest.raises(DBAPIError, match="clinical fact evidence is immutable"):
            db.commit()

    patient = auth_headers("patient")
    patient_status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=patient)
    assert patient_status.status_code == 200
    assert patient_status.json()["patient_summary"]
    assert patient_status.json()["current_transcript_revision_id"] is None
    assert "penicillin allergy during review" not in patient_status.text


def test_reanalysis_job_is_bound_to_payload_revision(
    client: TestClient, auth_headers
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(job_id)
    reanalysis = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "tampered-revision-binding"},
    )
    assert reanalysis.status_code == 202
    tampered_job_id = uuid.UUID(reanalysis.json()["job_id"])
    with Session(engine) as db:
        job = db.get(Job, tampered_job_id)
        assert job is not None
        job.payload_ciphertext = field_codec.encrypt_json(
            job.clinic_id,
            "job.payload",
            job.id,
            {"session_id": session_id, "revision_id": str(uuid.uuid4())},
        )
        db.add(job)
        db.commit()
    _run_job(str(tampered_job_id))
    with Session(engine) as db:
        job = db.get(Job, tampered_job_id)
        voice_session = db.get(VoiceSession, uuid.UUID(session_id))
        assert job is not None and voice_session is not None
        assert job.state == "failed"
        assert job.error_code == "REANALYSIS_REVISION_NOT_FOUND"
        assert voice_session.state == "needs_review"
