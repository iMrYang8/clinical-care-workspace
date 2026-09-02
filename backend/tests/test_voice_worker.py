import asyncio
import hashlib
import io
import math
import struct
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import sleep

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.api.routes import ai as ai_routes
from app.core.config import settings
from app.core.db import engine
from app.core.field_crypto import field_codec
from app.core.security import create_access_token
from app.models import (
    AudioAsset,
    AudioChunk,
    ClinicalFact,
    ClinicMembership,
    ClinicOperationalSetting,
    Job,
    JobAttempt,
    ProvenancePointer,
    TranscriptRevision,
    TranscriptSegment,
    User,
    VoiceSession,
    get_datetime_utc,
)
from app.seed import demo_id
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
def test_trilingual_intrasentential_fixture_is_explicit() -> None:
    provider = SyntheticFixtureProvider()
    result = provider.transcribe_fixture("trilingual-intrasentential-v1")
    assert result.provider == "deterministic-synthetic-fixture"
    assert result.model == "trilingual-intrasentential-v1"
    assert [segment.speaker_id for segment in result.segments] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_02",
    ]
    assert {segment.detected_language for segment in result.segments} == {
        "en",
        "zh",
        "ms",
    }
    family = result.segments[2]
    assert "penicillin" in family.text
    assert "koe-bin" in family.text
    assert family.overlap_group_id is None
    assert "OVERLAP_REVIEW" not in result.warnings
    assert "SYNTHETIC_FIXTURE" in result.warnings
    validate_transcript_result(result)


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
        json={
            "client_device_id": "worker-fixture",
            "capture_role": "patient",
            "expected_patient_id": patient_id,
            "expected_capture_kind": "clinical",
        },
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
    assert status.json()["live_transcript_status"] == "replaced"
    assert status.json()["live_transcript_reason_code"] is None
    status_quality = status.json()["audio_quality"]
    assert status.json()["audio_quality_unavailable_reason"] is None
    assert status_quality["measurement_stage"] == "decoded-pre-normalization"
    assert status_quality["processing_chain_version"] == (
        "nightingale-voice-working-copy-v1"
    )
    assert status_quality["denoise_applied"] is True
    assert "device_signals" not in status_quality
    assert "normalized_output_signals" not in status_quality
    assert "denoise_filter" not in status_quality
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=headers
    )
    assert transcript.status_code == 200, transcript.text
    body = transcript.json()
    assert body["audio_quality"] == status_quality
    assert body["audio_quality_unavailable_reason"] is None
    assert body["provider"] == "deterministic-synthetic-fixture"
    assert [item["speaker_id"] for item in body["segments"]] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert {item["detected_language"] for item in body["segments"]} == {"en", "zh"}
    assert [
        [span["language_code"] for span in item["language_spans"]]
        for item in body["segments"]
    ] == [["en"], ["zh"]]
    assert all(
        item["text"][span["start_offset"] : span["end_offset"]] == item["text"]
        for item in body["segments"]
        for span in item["language_spans"]
    )
    assert {
        span["detection_source"]
        for item in body["segments"]
        for span in item["language_spans"]
    } == {"lexicon_and_provider"}
    assert body["segments"][1]["overlap_group_id"] == "overlap-1"
    assert body["facts"][0]["exact_quote"] == "penicillin allergy"
    audio = client.get(f"/api/v1/voice/sessions/{session_id}/audio", headers=headers)
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.headers["cache-control"] == "private, no-store"
    assert audio.content.startswith(b"RIFF")

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


def test_published_voice_session_cannot_retry_original_review_job(
    client: TestClient, auth_headers
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(job_id)
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert transcript.status_code == 200
    published = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": transcript.json()["id"]},
    )
    assert published.status_code == 200, published.text

    retried = client.post(f"/api/v1/jobs/{job_id}/retry", headers=clinician)
    assert retried.status_code == 409
    assert retried.json()["detail"]["code"] == "VOICE_ALREADY_PUBLISHED"
    status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=clinician)
    assert status.status_code == 200
    assert status.json()["state"] == "published"
    assert status.json()["published_entry_id"] == published.json()["entry_id"]


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
    ) -> tuple[TranscriptResult, None, None]:
        return (
            SyntheticFixtureProvider().transcribe_fixture("code-switch-overlap-v1"),
            None,
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


def test_worker_revocation_during_asr_fences_all_derived_writes(
    client: TestClient,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
    owner_session: Session,
) -> None:
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    job_uuid = uuid.UUID(job_id)
    session_uuid = uuid.UUID(session_id)
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()

    async def delayed_provider(
        *_args: object, **_kwargs: object
    ) -> tuple[TranscriptResult, None, None]:
        provider_entered.set()
        await release_provider.wait()
        return (
            SyntheticFixtureProvider().transcribe_fixture("code-switch-overlap-v1"),
            None,
            None,
        )

    monkeypatch.setattr(voice_worker, "_transcribe", delayed_provider)

    async def revoke_while_provider_is_in_flight() -> None:
        with Session(engine) as worker_db:
            job = worker_db.get(Job, job_uuid)
            assert job is not None
            context = worker_context_for_job(worker_db, job)
            assert context is not None
            processing = asyncio.create_task(
                process_voice_job(worker_db, context, job_uuid)
            )
            await asyncio.wait_for(provider_entered.wait(), timeout=10)
            membership = owner_session.get(
                ClinicMembership, demo_id("membership-worker")
            )
            assert membership is not None
            membership.is_active = False
            owner_session.add(membership)
            owner_session.commit()
            release_provider.set()
            with pytest.raises(HTTPException) as exc_info:
                await asyncio.wait_for(processing, timeout=10)
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail == {"code": "JOB_CLAIM_LOST"}

    asyncio.run(revoke_while_provider_is_in_flight())

    with Session(engine) as db:
        job = db.get(Job, job_uuid)
        voice_session = db.get(VoiceSession, session_uuid)
        attempts = db.exec(
            select(JobAttempt).where(JobAttempt.job_id == job_uuid)
        ).all()
        assert job is not None and voice_session is not None
        assert job.state == "running"
        assert voice_session.state == "transcribing"
        assert len(attempts) == 1
        assert attempts[0].status == "started"
        assert db.exec(select(TranscriptRevision)).all() == []
        assert db.exec(select(ClinicalFact)).all() == []


def test_worker_revocation_during_preprocessing_rolls_back_audio_asset(
    client: TestClient,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
    owner_session: Session,
) -> None:
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    job_uuid = uuid.UUID(job_id)
    session_uuid = uuid.UUID(session_id)
    preprocessing_entered = Event()
    release_preprocessing = Event()
    original_store_asset = voice_worker._store_asset

    def blocked_store_asset(*args: object, **kwargs: object) -> AudioAsset:
        preprocessing_entered.set()
        if not release_preprocessing.wait(timeout=10):
            raise TimeoutError("test did not release preprocessing")
        return original_store_asset(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(voice_worker, "_store_asset", blocked_store_asset)

    with ThreadPoolExecutor(max_workers=1) as pool:
        processing = pool.submit(_run_job, job_id)
        assert preprocessing_entered.wait(timeout=10)
        membership = owner_session.get(ClinicMembership, demo_id("membership-worker"))
        assert membership is not None
        membership.is_active = False
        owner_session.add(membership)
        owner_session.commit()
        release_preprocessing.set()
        with pytest.raises(HTTPException) as exc_info:
            processing.result(timeout=15)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == {"code": "JOB_CLAIM_LOST"}

    with Session(engine) as db:
        job = db.get(Job, job_uuid)
        voice_session = db.get(VoiceSession, session_uuid)
        attempts = db.exec(
            select(JobAttempt).where(JobAttempt.job_id == job_uuid)
        ).all()
        assert job is not None and voice_session is not None
        assert job.state == "running"
        assert voice_session.state == "preprocessing"
        assert len(attempts) == 1
        assert attempts[0].status == "started"
        assert db.exec(select(AudioAsset)).all() == []


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
    ) -> tuple[TranscriptResult, None, None]:
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
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=headers,
        json={"expected_revision_id": transcript.json()["id"]},
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


def test_exhausted_voice_lease_moves_session_to_review_without_provider_reentry(
    client: TestClient,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
    owner_session: Session,
) -> None:
    import app.ai_worker as ai_worker

    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    job_uuid = uuid.UUID(job_id)
    session_uuid = uuid.UUID(session_id)
    token = uuid.uuid4()
    job = owner_session.get(Job, job_uuid)
    voice_session = owner_session.get(VoiceSession, session_uuid)
    assert job is not None and voice_session is not None
    job.state = "running"
    job.attempt_count = job.max_attempts
    job.locked_by = str(token)
    job.locked_until = get_datetime_utc() - timedelta(seconds=1)
    voice_session.state = "transcribing"
    owner_session.add(job)
    owner_session.add(voice_session)
    owner_session.add(
        JobAttempt(
            id=token,
            clinic_id=job.clinic_id,
            job_id=job.id,
            worker_membership_id=demo_id("membership-worker"),
            attempt_no=job.max_attempts,
        )
    )
    owner_session.commit()

    async def provider_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exhausted voice job must not re-enter processing")

    monkeypatch.setattr(ai_worker, "process_voice_job", provider_must_not_run)
    assert asyncio.run(ai_worker.run_once()) == 0

    with Session(engine) as db:
        terminal_job = db.get(Job, job_uuid)
        terminal_session = db.get(VoiceSession, session_uuid)
        assert terminal_job is not None and terminal_session is not None
        assert terminal_job.state == "failed"
        assert terminal_job.error_code == "JOB_ATTEMPTS_EXHAUSTED"
        assert terminal_session.state == "needs_review"
        assert terminal_session.error_code == "VOICE_WORKER_ATTEMPTS_EXHAUSTED"
        assert "VOICE_WORKER_ATTEMPTS_EXHAUSTED" in (
            terminal_session.warning_codes_json
        )


def test_invalid_encrypted_voice_payload_still_terminalizes_attempt(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    job_uuid = uuid.UUID(job_id)
    session_uuid = uuid.UUID(session_id)
    job = owner_session.get(Job, job_uuid)
    assert job is not None
    job.payload_ciphertext = b"invalid-encrypted-payload"
    owner_session.add(job)
    owner_session.commit()

    _run_job(job_id)

    with Session(engine) as db:
        retryable_job = db.get(Job, job_uuid)
        retryable_session = db.get(VoiceSession, session_uuid)
        assert retryable_job is not None and retryable_session is not None
        assert retryable_job.state == "failed"
        assert retryable_job.attempt_count == 1
        assert retryable_job.next_run_at is None
        assert retryable_session.state == "needs_review"
        assert retryable_session.error_code == "VOICE_JOB_FAILED"
        encrypted_chunk = db.exec(
            select(AudioChunk).where(AudioChunk.session_id == session_uuid)
        ).one()
        assert encrypted_chunk.payload_ciphertext[:1] == b"\x01"

    # A permanent/malformed failure has no scheduled retry and cannot spin in
    # the poller. Each further attempt requires an explicit clinical action.
    with pytest.raises(HTTPException) as not_claimable:
        _run_job(job_id)
    assert not_claimable.value.detail == {"code": "JOB_NOT_CLAIMABLE"}

    for expected_attempt in range(2, 7):
        retried = client.post(f"/api/v1/jobs/{job_id}/retry", headers=clinician)
        assert retried.status_code == 200, retried.text
        assert retried.json()["state"] == "pending"
        _run_job(job_id)
        with Session(engine) as db:
            failed = db.get(Job, job_uuid)
            assert failed is not None
            assert failed.attempt_count == expected_attempt

    with Session(engine) as db:
        terminal_job = db.get(Job, job_uuid)
        terminal_session = db.get(VoiceSession, session_uuid)
        assert terminal_job is not None and terminal_session is not None
        assert terminal_job.state == "failed"
        assert terminal_job.error_code == "VOICE_JOB_FAILED"
        assert terminal_job.attempt_count == terminal_job.max_attempts == 6
        assert terminal_session.state == "needs_review"
        assert terminal_session.error_code == "VOICE_JOB_FAILED"
        assert "VOICE_WORKER_ATTEMPTS_EXHAUSTED" in (
            terminal_session.warning_codes_json
        )


def test_code_switched_language_spans_persist_and_survive_api_reload(
    client: TestClient, auth_headers
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(job_id)
    initial = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert initial.status_code == 200, initial.text

    source_text = (
        "Started metformin 500mg. "
        "Pesakit mula aspirin 100mg secara oral dua kali sehari."
    )
    corrected = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={
            "expected_revision_id": initial.json()["id"],
            "text": source_text,
        },
    )
    assert corrected.status_code == 201, corrected.text
    segment = corrected.json()["segments"][0]
    assert segment["text"] == source_text
    assert segment["source_language"] == "und"
    assert [span["language_code"] for span in segment["language_spans"]] == [
        "en",
        "ms",
    ]
    assert [
        source_text[span["start_offset"] : span["end_offset"]]
        for span in segment["language_spans"]
    ] == [
        "Started metformin 500mg.",
        " Pesakit mula aspirin 100mg secara oral dua kali sehari.",
    ]

    revision_id = uuid.UUID(corrected.json()["id"])
    with Session(engine) as db:
        stored = db.exec(
            select(TranscriptSegment).where(
                TranscriptSegment.revision_id == revision_id
            )
        ).one()
        assert stored.language_spans_json == segment["language_spans"]
        assert (
            field_codec.decrypt_text(
                stored.clinic_id,
                "transcript_segment.text",
                stored.id,
                stored.text_ciphertext,
            )
            == source_text
        )

    reloaded = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["segments"][0] == segment


def test_clinic_language_policy_fails_closed_without_rewriting_code_switch(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    clinician = auth_headers("clinician")
    patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(job_id)
    initial = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert initial.status_code == 200, initial.text

    operational = owner_session.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == demo_id("clinic-primary")
        )
    ).one()
    operational.supported_languages_json = ["en"]
    owner_session.add(operational)
    owner_session.commit()

    source_text = (
        "Patient is allergic to penicillin; "
        "pesakit alahan kepada amoksisilin; "
        "tùi aspirin kòe-bín."
    )
    corrected = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={
            "expected_revision_id": initial.json()["id"],
            "text": source_text,
        },
    )
    assert corrected.status_code == 201, corrected.text
    corrected_body = corrected.json()
    assert corrected_body["text"] == source_text
    spans = corrected_body["segments"][0]["language_spans"]
    assert [item["language_code"] for item in spans] == ["en", "ms", "nan"]
    assert [item["review_required"] for item in spans] == [False, True, True]
    assert [
        source_text[item["start_offset"] : item["end_offset"]] for item in spans
    ] == [
        "Patient is allergic to penicillin;",
        " pesakit alahan kepada amoksisilin;",
        " tùi aspirin kòe-bín.",
    ]
    assert "CLINIC_LANGUAGE_POLICY_REVIEW_REQUIRED" in corrected_body["warning_codes"]

    queued = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "clinic-language-policy-reanalysis"},
        json={"expected_revision_id": corrected_body["id"]},
    )
    assert queued.status_code == 202, queued.text
    _run_job(queued.json()["job_id"])
    reviewed = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["text"] == source_text
    assert {
        (item["exact_quote"], item["value"])
        for item in reviewed.json()["facts"]
        if item["fact_type"] == "allergy"
    } == {
        ("allergic to penicillin", "penicillin allergy:present"),
        ("alahan kepada amoksisilin", "amoxicillin allergy:unknown"),
        ("tùi aspirin kòe-bín", "aspirin allergy:unknown"),
    }

    published = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": reviewed.json()["id"]},
    )
    assert published.status_code == 200, published.text
    assertions = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts", headers=clinician
    )
    assert assertions.status_code == 200, assertions.text
    voice_assertions = [item for item in assertions.json() if item["origin"] == "voice"]
    by_language = {item["source_language"]: item for item in voice_assertions}
    assert by_language["en"]["clinical_status"] == "active"
    assert by_language["ms"]["clinical_status"] == "review_required"
    assert by_language["nan"]["clinical_status"] == "review_required"


def test_voice_publish_requalifies_current_clinic_language_policy(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    clinician = auth_headers("clinician")
    patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(job_id)
    initial = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert initial.status_code == 200, initial.text

    source_text = (
        "Patient is allergic to penicillin; pesakit alahan kepada amoksisilin."
    )
    corrected = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={"expected_revision_id": initial.json()["id"], "text": source_text},
    )
    assert corrected.status_code == 201, corrected.text
    assert [
        item["review_required"]
        for item in corrected.json()["segments"][0]["language_spans"]
    ] == [False, False]

    queued = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "language-policy-publish-recheck"},
        json={"expected_revision_id": corrected.json()["id"]},
    )
    assert queued.status_code == 202, queued.text
    _run_job(queued.json()["job_id"])
    reviewed = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert reviewed.status_code == 200, reviewed.text
    assert {
        (item["exact_quote"], item["value"])
        for item in reviewed.json()["facts"]
        if item["fact_type"] == "allergy"
    } == {
        ("allergic to penicillin", "penicillin allergy:present"),
        ("alahan kepada amoksisilin", "amoxicillin allergy:present"),
    }

    # Policy changes are effective at use time, not only at transcription.
    operational = owner_session.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == demo_id("clinic-primary")
        )
    ).one()
    operational.supported_languages_json = ["en"]
    owner_session.add(operational)
    owner_session.commit()

    published = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": reviewed.json()["id"]},
    )
    assert published.status_code == 200, published.text
    assertions = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts", headers=clinician
    )
    assert assertions.status_code == 200, assertions.text
    voice_assertions = [item for item in assertions.json() if item["origin"] == "voice"]
    by_language = {item["source_language"]: item for item in voice_assertions}
    assert by_language["en"]["clinical_status"] == "active"
    assert by_language["ms"]["clinical_status"] == "review_required"


def test_correction_marks_stale_reanalysis_restores_provenance_and_publish(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(job_id)
    initial_transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert initial_transcript.status_code == 200
    initial_revision_id = initial_transcript.json()["id"]

    reviewer_user = User(
        email="second-clinician@nightingale.example",
        full_name="Second Synthetic Clinician",
        hashed_password="not-used-for-token-test",
    )
    owner_session.add(reviewer_user)
    owner_session.flush()
    reviewer_membership = ClinicMembership(
        clinic_id=demo_id("clinic-primary"),
        user_id=reviewer_user.id,
        role="clinician",
    )
    owner_session.add(reviewer_membership)
    owner_session.commit()
    reviewer = {
        "Authorization": "Bearer "
        + create_access_token(
            reviewer_user.id,
            timedelta(minutes=10),
            membership_id=reviewer_membership.id,
            clinic_id=reviewer_membership.clinic_id,
        )
    }
    empty = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=reviewer,
        json={
            "expected_revision_id": initial_revision_id,
            "text": " \n\t ",
        },
    )
    assert empty.status_code == 422
    assert empty.json()["detail"]["code"] == "TRANSCRIPT_CORRECTION_EMPTY"
    with Session(engine) as db:
        assert len(db.exec(select(TranscriptRevision)).all()) == 1
    corrected = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=reviewer,
        json={
            "expected_revision_id": initial_revision_id,
            "text": "Patient confirms a penicillin allergy during review.",
        },
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["stale"] is True
    assert "DOWNSTREAM_RESULTS_STALE" in corrected.json()["warning_codes"]
    assert corrected.json()["segments"][0]["language_spans"] == [
        {
            "start_offset": 0,
            "end_offset": len("Patient confirms a penicillin allergy during review."),
            "language_code": "en",
            "confidence": None,
            "detection_source": "lexicon_rule",
            "review_required": False,
        }
    ]
    stale_correction = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={
            "expected_revision_id": initial_revision_id,
            "text": "A stale editor must not replace the second clinician's work.",
        },
    )
    assert stale_correction.status_code == 409
    assert stale_correction.json()["detail"]["code"] == ("TRANSCRIPT_REVISION_CONFLICT")
    with Session(engine) as db:
        assert len(db.exec(select(TranscriptRevision)).all()) == 2
        voice_session = db.get(VoiceSession, uuid.UUID(session_id))
        assert voice_session is not None
        assert voice_session.current_transcript_revision_id == uuid.UUID(
            corrected.json()["id"]
        )
    blocked = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=reviewer,
        json={"expected_revision_id": corrected.json()["id"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "DOWNSTREAM_RESULTS_STALE"

    reanalyze = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=reviewer | {"Idempotency-Key": "corrected-reanalysis-v1"},
        json={"expected_revision_id": corrected.json()["id"]},
    )
    assert reanalyze.status_code == 202, reanalyze.text
    replay = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=reviewer | {"Idempotency-Key": "corrected-reanalysis-v1"},
        json={"expected_revision_id": corrected.json()["id"]},
    )
    assert replay.status_code == 202
    assert replay.json()["job_id"] == reanalyze.json()["job_id"]
    correction_race = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=reviewer,
        json={
            "expected_revision_id": corrected.json()["id"],
            "text": "A racing correction must be rejected.",
        },
    )
    assert correction_race.status_code == 409
    assert correction_race.json()["detail"]["code"] == "VOICE_REVIEW_STATE_CONFLICT"
    publish_race = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": corrected.json()["id"]},
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

    completed_replay = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=reviewer | {"Idempotency-Key": "corrected-reanalysis-v1"},
        json={"expected_revision_id": corrected.json()["id"]},
    )
    assert completed_replay.status_code == 202
    assert completed_replay.json()["job_id"] == reanalyze.json()["job_id"]
    stale_new_reanalysis = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "stale-new-reanalysis"},
        json={"expected_revision_id": corrected.json()["id"]},
    )
    assert stale_new_reanalysis.status_code == 409
    assert stale_new_reanalysis.json()["detail"]["code"] == (
        "TRANSCRIPT_REVISION_CONFLICT"
    )

    stale_page_publish = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": initial_revision_id},
    )
    assert stale_page_publish.status_code == 409
    assert stale_page_publish.json()["detail"]["code"] == (
        "TRANSCRIPT_REVISION_CONFLICT"
    )

    published = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=reviewer,
        json={"expected_revision_id": reviewed.json()["id"]},
    )
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
    replayed_publication = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=reviewer,
        json={"expected_revision_id": reviewed.json()["id"]},
    )
    assert replayed_publication.status_code == 200
    assert replayed_publication.json()["entry_id"] == published.json()["entry_id"]
    stale_published_replay = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": initial_revision_id},
    )
    assert stale_published_replay.status_code == 409
    assert stale_published_replay.json()["detail"]["code"] == (
        "TRANSCRIPT_REVISION_CONFLICT"
    )
    published_correction = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=reviewer,
        json={
            "expected_revision_id": reviewed.json()["id"],
            "text": "Published transcripts are immutable through this API.",
        },
    )
    assert published_correction.status_code == 409
    published_reanalysis = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=reviewer | {"Idempotency-Key": "after-publish"},
        json={"expected_revision_id": reviewed.json()["id"]},
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
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert transcript.status_code == 200
    reanalysis = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "tampered-revision-binding"},
        json={"expected_revision_id": transcript.json()["id"]},
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
        assert voice_session.state == "extracting"
        # Simulate a legacy/pre-fix retryable failure row. Manual retry must
        # restore the CAS barrier in the same transaction as Job=pending.
        voice_session.state = "needs_review"
        db.add(voice_session)
        db.commit()

    retried = client.post(f"/api/v1/jobs/{tampered_job_id}/retry", headers=clinician)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "pending"
    status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=clinician)
    assert status.status_code == 200
    assert status.json()["state"] == "extracting"
    blocked_publish = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": transcript.json()["id"]},
    )
    assert blocked_publish.status_code == 409
    assert blocked_publish.json()["detail"]["code"] == "VOICE_NOT_PUBLISHABLE"


def test_retryable_reanalysis_failure_keeps_cas_barrier_until_auto_retry(
    client: TestClient,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, initial_job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(initial_job_id)
    initial = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert initial.status_code == 200
    initial_revision_id = initial.json()["id"]
    queued = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "transient-reanalysis"},
        json={"expected_revision_id": initial_revision_id},
    )
    assert queued.status_code == 202, queued.text
    reanalysis_job_id = queued.json()["job_id"]

    original_create_revision = voice_worker._create_revision
    failed_once = False

    def flaky_create_revision(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get("reanalyzed") and not failed_once:
            failed_once = True
            raise RuntimeError("synthetic transient extraction failure")
        return original_create_revision(*args, **kwargs)

    monkeypatch.setattr(voice_worker, "_create_revision", flaky_create_revision)
    _run_job(reanalysis_job_id)

    with Session(engine) as db:
        failed_job = db.get(Job, uuid.UUID(reanalysis_job_id))
        voice_session = db.get(VoiceSession, uuid.UUID(session_id))
        revisions = db.exec(
            select(TranscriptRevision).where(
                TranscriptRevision.session_id == uuid.UUID(session_id)
            )
        ).all()
        assert failed_job is not None and voice_session is not None
        assert failed_job.state == "failed"
        assert failed_job.attempt_count == 1
        assert voice_session.state == "extracting"
        assert voice_session.current_transcript_revision_id == uuid.UUID(
            initial_revision_id
        )
        assert len(revisions) == 1

    blocked_publish = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=clinician,
        json={"expected_revision_id": initial_revision_id},
    )
    assert blocked_publish.status_code == 409
    assert blocked_publish.json()["detail"]["code"] == "VOICE_NOT_PUBLISHABLE"
    blocked_correction = client.post(
        f"/api/v1/voice/sessions/{session_id}/transcript/correct",
        headers=clinician,
        json={
            "expected_revision_id": initial_revision_id,
            "text": "This edit must wait for the durable retry.",
        },
    )
    assert blocked_correction.status_code == 409
    assert blocked_correction.json()["detail"]["code"] == (
        "VOICE_REVIEW_STATE_CONFLICT"
    )
    competing = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "competing-reanalysis"},
        json={"expected_revision_id": initial_revision_id},
    )
    assert competing.status_code == 409
    assert competing.json()["detail"]["code"] == "VOICE_REANALYSIS_IN_PROGRESS"

    # A generic extraction failure has no durable automatic retry schedule.
    # The old revision remains fenced until a clinician explicitly retries;
    # the next attempt then succeeds without exposing it between attempts.
    retried = client.post(f"/api/v1/jobs/{reanalysis_job_id}/retry", headers=clinician)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "pending"
    _run_job(reanalysis_job_id)
    reviewed = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["id"] != initial_revision_id
    with Session(engine) as db:
        completed_job = db.get(Job, uuid.UUID(reanalysis_job_id))
        voice_session = db.get(VoiceSession, uuid.UUID(session_id))
        attempts = db.exec(
            select(JobAttempt)
            .where(JobAttempt.job_id == uuid.UUID(reanalysis_job_id))
            .order_by(JobAttempt.attempt_no)
        ).all()
        assert completed_job is not None and voice_session is not None
        assert completed_job.state in {"completed", "needs_review"}
        assert completed_job.attempt_count == 2
        assert voice_session.state in {"ready", "needs_review"}
        assert [attempt.status for attempt in attempts] == ["failed", "completed"]


def test_manual_reanalysis_retry_and_publish_share_session_cas(
    client: TestClient,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
    owner_session: Session,
) -> None:
    clinician = auth_headers("clinician")
    _patient_id, session_id, initial_job_id = _create_recording(
        client, clinician, synthetic_fixture=True
    )
    _run_job(initial_job_id)
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert transcript.status_code == 200
    revision_id = transcript.json()["id"]
    queued = client.post(
        f"/api/v1/voice/sessions/{session_id}/reanalyze",
        headers=clinician | {"Idempotency-Key": "manual-retry-publish-cas"},
        json={"expected_revision_id": revision_id},
    )
    assert queued.status_code == 202
    job_id = uuid.UUID(queued.json()["job_id"])
    job = owner_session.get(Job, job_id)
    voice_session = owner_session.get(VoiceSession, uuid.UUID(session_id))
    assert job is not None and voice_session is not None
    job.state = "failed"
    job.error_code = "SYNTHETIC_TRANSIENT_FAILURE"
    job.attempt_count = 1
    voice_session.state = "needs_review"
    owner_session.add(job)
    owner_session.add(voice_session)
    owner_session.commit()

    retry_holds_session = Event()
    release_retry = Event()
    original_worker_context = ai_routes.worker_context_for_job

    def blocked_worker_context(*args, **kwargs):
        retry_holds_session.set()
        if not release_retry.wait(timeout=10):
            raise TimeoutError("test did not release retry transaction")
        return original_worker_context(*args, **kwargs)

    monkeypatch.setattr(ai_routes, "worker_context_for_job", blocked_worker_context)
    with ThreadPoolExecutor(max_workers=2) as pool:
        retry_future = pool.submit(
            client.post, f"/api/v1/jobs/{job_id}/retry", headers=clinician
        )
        assert retry_holds_session.wait(timeout=10)
        publish_future = pool.submit(
            client.post,
            f"/api/v1/voice/sessions/{session_id}/publish",
            headers=clinician,
            json={"expected_revision_id": revision_id},
        )
        sleep(0.15)
        assert publish_future.done() is False
        release_retry.set()
        retried = retry_future.result(timeout=10)
        publish = publish_future.result(timeout=10)

    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "pending"
    assert publish.status_code == 409, publish.text
    assert publish.json()["detail"]["code"] == "VOICE_NOT_PUBLISHABLE"
    status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=clinician)
    assert status.json()["state"] == "extracting"
