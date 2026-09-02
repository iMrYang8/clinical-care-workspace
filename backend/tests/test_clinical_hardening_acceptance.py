from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import struct
import uuid
import wave
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import desc
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.db import engine
from app.models import (
    AuditEvent,
    CalibrationReport,
    ClinicalFactAssertion,
    ClinicMembership,
    ClinicOperationalSetting,
    ConflictCase,
    DecisionAssessment,
    EvaluationRun,
    Highlight,
    ImportanceCandidateExposure,
    Job,
    NotificationOutbox,
    PatientPortalEvent,
    PatientPublication,
    PatientPublicationCorrectionCreate,
    ProvisionalSafetyAlert,
    PublicationCorrectionOutreach,
    User,
    VoiceSession,
    get_datetime_utc,
)
from app.seed import demo_id
from app.services.ai_jobs import worker_context_for_job
from app.services.decisioning import (
    evaluation_manifest_sha256,
    qualify_calibration_report,
    request_parameters_sha256,
)
from app.services.trust_evaluation import _persist_report
from app.services.voice import live as voice_live
from app.services.voice.live import persist_completed_safety_alerts
from app.services.voice.providers.base import TranscriptResult, TranscriptSegmentResult
from app.services.voice.providers.deterministic import SyntheticFixtureProvider
from app.services.voice.worker import process_voice_job


def _clinical_wav_fixture(duration_seconds: float = 11.0) -> bytes:
    sample_rate = 16_000
    frames = bytearray()
    for index in range(int(duration_seconds * sample_rate)):
        sample = int(4_000 * math.sin(2 * math.pi * 220 * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(frames)
    return output.getvalue()


def _create_clinical_highlight(
    client: TestClient,
    headers: dict[str, str],
    *,
    content: str = "prefix IMPORTANT suffix",
) -> tuple[dict[str, object], dict[str, object]]:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry_response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Clinical hardening source",
            "content": content,
            "patient_facing": False,
        },
    )
    assert entry_response.status_code == 201, entry_response.text
    entry = entry_response.json()
    highlight_response = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Addressable priority",
            "patient_facing": False,
        },
    )
    assert highlight_response.status_code == 201, highlight_response.text
    return entry, highlight_response.json()


def test_completed_mixed_language_alerts_are_addressable_and_require_review(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    created_session = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": True,
            "fixture_id": "code-switch-overlap-v1",
        },
    )
    assert created_session.status_code == 201, created_session.text
    voice_session = owner_session.get(
        VoiceSession, uuid.UUID(created_session.json()["id"])
    )
    assert voice_session is not None
    context = SimpleNamespace(
        clinic_id=voice_session.clinic_id,
        user_id=voice_session.created_by_id,
    )
    completed_at = get_datetime_utc()
    fixtures = (
        ("seg-en", "Patient is allergic to penicillin.", "en"),
        ("seg-ms", "Pesakit alahan kepada amoksisilin.", "ms"),
        ("seg-nan", "Tùi aspirin kòe-bín.", "nan"),
    )
    created_alerts: list[ProvisionalSafetyAlert] = []
    for event_id, text, language in fixtures:
        created_alerts.extend(
            persist_completed_safety_alerts(
                owner_session,
                context,  # type: ignore[arg-type]
                voice_session,
                source_event_id=event_id,
                text=text,
                source_language=language,
                completed_segment_at=completed_at,
            )
        )
    owner_session.commit()
    assert len(created_alerts) == 3
    assert all(
        alert.detected_at - alert.completed_segment_at < timedelta(seconds=5)
        for alert in created_alerts
    )

    listed = client.get(
        f"/api/v1/voice/sessions/{voice_session.id}/safety-alerts",
        headers=clinician,
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert {row["source_language"] for row in rows} == {"en", "ms", "nan"}
    assert all(row["state"] == "pending" for row in rows)
    assert all(row["source_end_offset"] > row["source_start_offset"] for row in rows)

    by_language = {row["source_language"]: row for row in rows}
    confirmed = client.post(
        f"/api/v1/voice/safety-alerts/{by_language['ms']['id']}/confirm",
        headers=clinician,
        json={"reason_code": "clinician_verified_source"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["state"] == "confirmed"
    assert confirmed.json()["confirmed_assertion_id"] is not None
    dismissed = client.post(
        f"/api/v1/voice/safety-alerts/{by_language['nan']['id']}/dismiss",
        headers=clinician,
        json={"reason_code": "clinician_rejected_source"},
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["state"] == "dismissed"

    owner_session.expire_all()
    assertion = owner_session.get(
        ClinicalFactAssertion,
        uuid.UUID(confirmed.json()["confirmed_assertion_id"]),
    )
    assert assertion is not None
    assert assertion.source_language == "ms"
    assert assertion.assertion_state == "active"
    nan_alert = owner_session.get(
        ProvisionalSafetyAlert, uuid.UUID(by_language["nan"]["id"])
    )
    assert nan_alert is not None
    assert nan_alert.confirmed_assertion_id is None


def test_one_code_switched_live_statement_reaches_conflict_glance_and_gate(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    operational = owner_session.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == demo_id("clinic-primary")
        )
    ).one()
    assert {"en", "ms", "nan"}.issubset(set(operational.supported_languages_json))

    broad_text = "No known drug allergies"
    broad_response = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Pre-existing broad allergy statement",
            "content": broad_text,
            "patient_facing": False,
        },
    )
    assert broad_response.status_code == 201, broad_response.text
    broad_entry = broad_response.json()
    highlight_response = client.post(
        f"/api/v1/entries/{broad_entry['id']}/highlights",
        headers=clinician,
        json={
            "entry_version_id": broad_entry["version_id"],
            "start_offset": 0,
            "end_offset": len(broad_text),
            "exact_quote": broad_text,
            "prefix": "",
            "suffix": "",
            "label": "Reconcile broad allergy statement",
            "patient_facing": False,
        },
    )
    assert highlight_response.status_code == 201, highlight_response.text
    highlight = highlight_response.json()
    accepted = client.post(
        f"/api/v1/highlights/{highlight['id']}/accept", headers=clinician
    )
    assert accepted.status_code == 200, accepted.text
    owner_session.expire_all()
    broad_assertion = owner_session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.source_entry_version_id
            == uuid.UUID(broad_entry["version_id"]),
            ClinicalFactAssertion.fact_type == "allergy",
            ClinicalFactAssertion.assertion_state == "active",
        )
    ).one()
    broad_assertion.highlight_id = uuid.UUID(highlight["id"])
    owner_session.add(broad_assertion)
    owner_session.commit()

    created_session = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": True,
            "fixture_id": "code-switch-overlap-v1",
        },
    )
    assert created_session.status_code == 201, created_session.text
    session_id = created_session.json()["id"]
    joined = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=clinician,
        json={
            "client_device_id": "one-code-switch-statement",
            "capture_role": "clinician",
            "expected_patient_id": patient_id,
            "expected_capture_kind": "clinical",
        },
    )
    assert joined.status_code == 201, joined.text
    voice_session = owner_session.get(VoiceSession, uuid.UUID(session_id))
    assert voice_session is not None and voice_session.state == "recording"
    context = SimpleNamespace(
        clinic_id=voice_session.clinic_id,
        user_id=voice_session.created_by_id,
    )
    completed_transcript = (
        "Patient is allergic to penicillin; "
        "pesakit alahan kepada amoksisilin; "
        "tùi aspirin kòe-bín."
    )
    alerts = persist_completed_safety_alerts(
        owner_session,
        context,  # type: ignore[arg-type]
        voice_session,
        source_event_id="one-en-ms-nan-completed-turn",
        text=completed_transcript,
        source_language="multilingual",
        completed_segment_at=get_datetime_utc(),
    )
    owner_session.commit()
    assert len(alerts) == 3
    assert {alert.source_language for alert in alerts} == {"en", "ms", "nan"}
    assert {alert.concept_code for alert in alerts} == {
        "allergy:penicillin",
        "allergy:amoxicillin",
        "allergy:aspirin",
    }
    assert all(alert.polarity == "present" for alert in alerts)
    assert {
        alert.source_language: completed_transcript[
            alert.source_start_offset : alert.source_end_offset
        ]
        for alert in alerts
    } == {
        "en": "allergic to penicillin",
        "ms": "alahan kepada amoksisilin",
        "nan": "tùi aspirin kòe-bín",
    }

    listed = client.get(
        f"/api/v1/voice/sessions/{session_id}/safety-alerts", headers=clinician
    )
    assert listed.status_code == 200, listed.text
    by_language = {row["source_language"]: row for row in listed.json()}

    # Finalization independently persists the exact same completed statement
    # as an immutable transcript revision with linked normalized facts. This
    # keeps the live alert provisional while making the end-to-end source
    # evidence reloadable through the clinical transcript API.
    audio_payload = _clinical_wav_fixture()
    uploaded = client.put(
        f"/api/v1/voice/sessions/{session_id}/devices/{joined.json()['id']}/chunks/0",
        headers=clinician
        | {
            "Content-Type": "audio/wav",
            "X-Chunk-SHA256": hashlib.sha256(audio_payload).hexdigest(),
            "X-Chunk-Start-Ms": "0",
            "X-Chunk-End-Ms": "11000",
        },
        content=audio_payload,
    )
    assert uploaded.status_code == 200, uploaded.text
    sealed = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices/{joined.json()['id']}/seal",
        headers=clinician,
        json={"last_chunk_index": 0},
    )
    assert sealed.status_code == 200, sealed.text

    def exact_code_switch_fixture(
        _provider: SyntheticFixtureProvider, fixture_id: str
    ) -> TranscriptResult:
        assert fixture_id == "code-switch-overlap-v1"
        return TranscriptResult(
            text=completed_transcript,
            segments=[
                TranscriptSegmentResult(
                    text=completed_transcript,
                    start_ms=0,
                    end_ms=9_000,
                    speaker_id="SPEAKER_00",
                    speaker_ids=("SPEAKER_00",),
                    detected_language="multilingual",
                    source_language="multilingual",
                    language_confidence=None,
                    confidence=0.91,
                    confidence_source="deterministic_fixture",
                    overlap_group_id=None,
                    text_start=0,
                    text_end=len(completed_transcript),
                )
            ],
            provider="deterministic-synthetic-fixture",
            model=fixture_id,
            detected_language="multilingual",
        )

    monkeypatch.setattr(
        SyntheticFixtureProvider,
        "transcribe_fixture",
        exact_code_switch_fixture,
    )
    finalized = client.post(
        f"/api/v1/voice/sessions/{session_id}/finalize",
        headers=clinician | {"Idempotency-Key": "one-code-switch-finalize"},
        json={"devices": [{"device_id": joined.json()["id"], "last_chunk_index": 0}]},
    )
    assert finalized.status_code == 202, finalized.text
    with Session(engine) as worker_db:
        job = worker_db.get(Job, uuid.UUID(finalized.json()["job_id"]))
        assert job is not None
        worker_context = worker_context_for_job(worker_db, job)
        assert worker_context is not None
        asyncio.run(process_voice_job(worker_db, worker_context, job.id))

    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=clinician
    )
    assert transcript.status_code == 200, transcript.text
    transcript_body = transcript.json()
    assert transcript_body["text"] == completed_transcript
    assert [
        item["language_code"]
        for item in transcript_body["segments"][0]["language_spans"]
    ] == ["en", "ms", "nan"]
    assert all(
        item["review_required"] is False
        for item in transcript_body["segments"][0]["language_spans"]
    )
    assert {
        (item["fact_type"], item["exact_quote"], item["value"])
        for item in transcript_body["facts"]
    } == {
        ("allergy", "allergic to penicillin", "penicillin allergy:present"),
        ("allergy", "alahan kepada amoksisilin", "amoxicillin allergy:present"),
        ("allergy", "tùi aspirin kòe-bín", "aspirin allergy:present"),
    }

    confirmed = client.post(
        f"/api/v1/voice/safety-alerts/{by_language['en']['id']}/confirm",
        headers=clinician,
        json={"reason_code": "clinician_verified_source"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed_assertion_id"] is not None

    conflicts = client.get(
        f"/api/v1/patients/{patient_id}/conflicts", headers=clinician
    )
    assert conflicts.status_code == 200, conflicts.text
    unresolved = [row for row in conflicts.json() if row["status"] == "unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0]["severity"] == "critical"
    assert {
        unresolved[0]["left_source_language"],
        unresolved[0]["right_source_language"],
    } == {"en"}

    glance = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert glance.status_code == 200, glance.text
    protected = next(
        row
        for row in glance.json()["review_cards"]
        if row["highlight_id"] == highlight["id"]
    )
    assert protected["critical"] is True

    blocked = client.post(
        f"/api/v1/entries/{broad_entry['id']}/patient-publications",
        headers=clinician,
        json={"entry_version_id": broad_entry["version_id"]},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "UNRESOLVED_CLINICAL_CONFLICT"


def test_twenty_minute_live_stream_surfaces_minute_two_alert_by_second_125(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    created_session = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": True,
            "fixture_id": "code-switch-overlap-v1",
        },
    )
    assert created_session.status_code == 201, created_session.text
    session_id = created_session.json()["id"]
    joined = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=clinician,
        json={
            "client_device_id": "twenty-minute-fake-stream",
            "capture_role": "clinician",
            "expected_patient_id": patient_id,
            "expected_capture_kind": "clinical",
        },
    )
    assert joined.status_code == 201, joined.text
    voice_session = owner_session.get(VoiceSession, uuid.UUID(session_id))
    assert voice_session is not None and voice_session.state == "recording"
    context = SimpleNamespace(
        clinic_id=voice_session.clinic_id,
        user_id=voice_session.created_by_id,
    )
    started_at = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    clock = {"now": started_at}
    monkeypatch.setattr(voice_live, "get_datetime_utc", lambda: clock["now"])

    for elapsed_seconds in (0, 60):
        clock["now"] = started_at + timedelta(seconds=elapsed_seconds)
        assert not persist_completed_safety_alerts(
            owner_session,
            context,  # type: ignore[arg-type]
            voice_session,
            source_event_id=f"turn-{elapsed_seconds}",
            text="Routine completed consultation turn.",
            source_language="en",
            completed_segment_at=clock["now"],
        )

    allergy_completed_at = started_at + timedelta(seconds=120)
    clock["now"] = started_at + timedelta(seconds=124)
    created_alerts = persist_completed_safety_alerts(
        owner_session,
        context,  # type: ignore[arg-type]
        voice_session,
        source_event_id="turn-120-allergy",
        text="Patient is allergic to penicillin.",
        source_language="en",
        completed_segment_at=allergy_completed_at,
    )
    owner_session.commit()
    assert len(created_alerts) == 1
    assert created_alerts[0].detected_at <= allergy_completed_at + timedelta(seconds=5)

    visible = client.get(
        f"/api/v1/voice/sessions/{session_id}/safety-alerts", headers=clinician
    )
    assert visible.status_code == 200, visible.text
    assert len(visible.json()) == 1
    detected_at = datetime.fromisoformat(visible.json()[0]["detected_at"])
    assert detected_at <= allergy_completed_at + timedelta(seconds=5)
    still_recording = client.get(
        f"/api/v1/voice/sessions/{session_id}", headers=clinician
    )
    assert still_recording.status_code == 200, still_recording.text
    assert still_recording.json()["state"] == "recording"

    for elapsed_seconds in range(180, 1_201, 60):
        clock["now"] = started_at + timedelta(seconds=elapsed_seconds)
        assert not persist_completed_safety_alerts(
            owner_session,
            context,  # type: ignore[arg-type]
            voice_session,
            source_event_id=f"turn-{elapsed_seconds}",
            text="Routine completed consultation turn.",
            source_language="en",
            completed_segment_at=clock["now"],
        )
    owner_session.commit()
    twenty_minutes = client.get(
        f"/api/v1/voice/sessions/{session_id}", headers=clinician
    )
    assert twenty_minutes.status_code == 200, twenty_minutes.text
    assert twenty_minutes.json()["state"] == "recording"


def test_remote_audio_consent_is_explicit_addressable_and_audited(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    created = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": True,
            "fixture_id": "code-switch-overlap-v1",
            "remote_audio_consent": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["remote_audio_consent_recorded"] is True
    assert created.json()["remote_audio_consent_at"] is not None

    revoked = client.put(
        f"/api/v1/voice/sessions/{created.json()['id']}/remote-audio-consent",
        headers=clinician,
        json={"consent": False},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["remote_audio_consent_recorded"] is False
    assert revoked.json()["remote_audio_consent_at"] is None

    actions = set(
        owner_session.exec(
            select(AuditEvent.action).where(
                AuditEvent.resource_id == uuid.UUID(created.json()["id"])
            )
        ).all()
    )
    assert "voice.remote_audio_consent_recorded" in actions
    assert "voice.remote_audio_consent_revoked" in actions


@pytest.mark.parametrize(
    "contents",
    [
        ("No known drug allergies", "Patient is allergic to penicillin."),
        ("Patient is allergic to penicillin.", "No known drug allergies"),
    ],
)
def test_generic_nkda_conflict_is_critical_in_both_insertion_orders(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    contents: tuple[str, str],
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    entries: list[dict[str, object]] = []
    for index, content in enumerate(contents):
        created = client.post(
            "/api/v1/entries",
            headers=clinician,
            json={
                "patient_id": patient_id,
                "section": "clinician",
                "title": f"Allergy source {index}",
                "content": content,
                "patient_facing": False,
            },
        )
        assert created.status_code == 201, created.text
        entries.append(created.json())

    response = client.get(f"/api/v1/patients/{patient_id}/conflicts", headers=clinician)
    assert response.status_code == 200, response.text
    unresolved = [row for row in response.json() if row["status"] == "unresolved"]
    assert len(unresolved) == 1
    conflict = unresolved[0]
    assert conflict["severity"] == "critical"
    assert conflict["review_required"] is True
    assert {conflict["left_assertion_scope"], conflict["right_assertion_scope"]} == {
        "drug_allergies",
        "specific_substance",
    }
    assert {conflict["left_polarity"], conflict["right_polarity"]} == {
        "absent",
        "present",
    }
    assert {
        conflict["left_allergy_category"],
        conflict["right_allergy_category"],
    } == {"drug"}
    assert {conflict["left_origin"], conflict["right_origin"]} == {"human"}
    assert {conflict["left_source_role"], conflict["right_source_role"]} == {
        "clinician"
    }
    assert {conflict["left_source_language"], conflict["right_source_language"]} == {
        "en"
    }
    assert {conflict["left_assertion_state"], conflict["right_assertion_state"]} == {
        "active"
    }
    assert conflict["left_recorded_at"] is not None
    assert conflict["right_recorded_at"] is not None

    owner_session.expire_all()
    stored = owner_session.get(ConflictCase, uuid.UUID(conflict["id"]))
    assert stored is not None
    assert str(stored.left_assertion_id) < str(stored.right_assertion_id)

    edited = client.patch(
        f"/api/v1/entries/{entries[0]['id']}",
        headers=clinician | {"If-Match": str(entries[0]["version_id"])},
        json={"content": "Allergies not documented"},
    )
    assert edited.status_code == 200, edited.text
    after = client.get(f"/api/v1/patients/{patient_id}/conflicts", headers=clinician)
    assert after.status_code == 200, after.text
    assert not [row for row in after.json() if row["status"] == "unresolved"]
    assert any(row["status"] == "superseded" for row in after.json())
    superseded_conflict = next(
        row for row in after.json() if row["status"] == "superseded"
    )
    assert "superseded" in {
        superseded_conflict["left_assertion_state"],
        superseded_conflict["right_assertion_state"],
    }
    facts = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts", headers=clinician
    )
    assert facts.status_code == 200, facts.text
    assert any(row["assertion_state"] == "superseded" for row in facts.json())
    assert any(
        row["assertion_state"] == "active" and row["polarity"] == "unknown"
        for row in facts.json()
    )


@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize(
    ("broad_text", "substance", "category", "expected_conflict"),
    [
        ("No known drug allergies", "penicillin", "drug", True),
        ("No known drug allergies", "peanut", "food", False),
        ("No known drug allergies", "latex", "environmental", False),
        ("No known allergies", "peanut", "food", True),
        ("No known allergies", "latex", "environmental", True),
    ],
)
def test_allergy_category_and_scope_are_stable_in_both_insertion_orders(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    broad_text: str,
    substance: str,
    category: str,
    expected_conflict: bool,
    reverse_order: bool,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    named_text = f"Patient is allergic to {substance}."
    contents = (named_text, broad_text) if reverse_order else (broad_text, named_text)
    for index, content in enumerate(contents):
        response = client.post(
            "/api/v1/entries",
            headers=clinician,
            json={
                "patient_id": patient_id,
                "section": "clinician",
                "title": f"Category-scoped allergy source {index}",
                "content": content,
                "patient_facing": False,
            },
        )
        assert response.status_code == 201, response.text

    conflicts = client.get(
        f"/api/v1/patients/{patient_id}/conflicts", headers=clinician
    )
    assert conflicts.status_code == 200, conflicts.text
    unresolved = [
        row
        for row in conflicts.json()
        if row["fact_type"] == "allergy" and row["status"] == "unresolved"
    ]
    assert bool(unresolved) is expected_conflict

    facts_response = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts", headers=clinician
    )
    assert facts_response.status_code == 200, facts_response.text
    facts = [
        row
        for row in facts_response.json()
        if row["fact_type"] == "allergy" and row["assertion_state"] == "active"
    ]
    named = next(row for row in facts if row["subject"] == substance)
    expected_broad_subject = "*drug" if broad_text == "No known drug allergies" else "*"
    broad = next(row for row in facts if row["subject"] == expected_broad_subject)
    assert named["allergy_category"] == category
    assert broad["allergy_category"] == (
        "drug" if broad["assertion_scope"] == "drug_allergies" else None
    )

    if expected_conflict:
        assert len(unresolved) == 1
        conflict = unresolved[0]
        assert conflict["severity"] == "critical"
        assert conflict["review_required"] is True
        assert {conflict["left_polarity"], conflict["right_polarity"]} == {
            "absent",
            "present",
        }
        assert {
            conflict["left_assertion_state"],
            conflict["right_assertion_state"],
        } == {"active"}
        assert {conflict["left_origin"], conflict["right_origin"]} == {"human"}
        expected_categories = {
            category,
            "drug" if broad["assertion_scope"] == "drug_allergies" else None,
        }
        assert {
            conflict["left_allergy_category"],
            conflict["right_allergy_category"],
        } == expected_categories


def test_invalid_allergy_category_is_rejected_by_the_persistence_boundary(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    response = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Audited category persistence",
            "content": "Patient is allergic to penicillin.",
            "patient_facing": False,
        },
    )
    assert response.status_code == 201, response.text
    facts = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts", headers=clinician
    )
    assert facts.status_code == 200, facts.text
    assertion = next(
        row
        for row in facts.json()
        if row["fact_type"] == "allergy" and row["subject"] == "penicillin"
    )
    assert assertion["allergy_category"] == "drug"

    with pytest.raises(IntegrityError):
        owner_session.execute(
            sql_text(
                "UPDATE clinical_fact_assertions "
                "SET allergy_category = 'device' WHERE id = :assertion_id"
            ),
            {"assertion_id": assertion["id"]},
        )
        owner_session.commit()
    owner_session.rollback()

    after = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts", headers=clinician
    )
    assert after.status_code == 200, after.text
    stored = next(row for row in after.json() if row["id"] == assertion["id"])
    assert stored["allergy_category"] == "drug"


def test_resolving_linked_allergy_conflict_rebuilds_glance_safety_state(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    content = "Patient is allergic to penicillin."
    left_response = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Addressable allergy source",
            "content": content,
            "patient_facing": False,
        },
    )
    assert left_response.status_code == 201, left_response.text
    left = left_response.json()
    highlight_response = client.post(
        f"/api/v1/entries/{left['id']}/highlights",
        headers=clinician,
        json={
            "entry_version_id": left["version_id"],
            "start_offset": 0,
            "end_offset": len(content),
            "exact_quote": content,
            "prefix": "",
            "suffix": "",
            "label": "Verify allergy source",
            "patient_facing": False,
        },
    )
    assert highlight_response.status_code == 201, highlight_response.text
    highlight = highlight_response.json()
    accepted = client.post(
        f"/api/v1/highlights/{highlight['id']}/accept", headers=clinician
    )
    assert accepted.status_code == 200, accepted.text

    # Bind the extracted allergy assertion to the exact source highlight. This
    # models the normal extraction/highlight linkage without changing either
    # immutable source pointer.
    allergy_assertion = owner_session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.source_entry_version_id
            == uuid.UUID(left["version_id"]),
            ClinicalFactAssertion.fact_type == "allergy",
            ClinicalFactAssertion.assertion_state == "active",
        )
    ).one()
    allergy_assertion.highlight_id = uuid.UUID(highlight["id"])
    owner_session.add(allergy_assertion)
    owner_session.commit()

    right_response = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Conflicting allergy source",
            "content": "No known drug allergies",
            "patient_facing": False,
        },
    )
    assert right_response.status_code == 201, right_response.text
    conflicts = client.get(
        f"/api/v1/patients/{patient_id}/conflicts", headers=clinician
    )
    unresolved = [row for row in conflicts.json() if row["status"] == "unresolved"]
    assert len(unresolved) == 1

    before = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert before.status_code == 200, before.text
    protected = next(
        row
        for row in before.json()["review_cards"]
        if row["highlight_id"] == highlight["id"]
    )
    assert protected["critical"] is True

    correction = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Clinician allergy reconciliation",
            "content": "Source records reviewed and reconciled by the clinician.",
            "patient_facing": False,
        },
    )
    assert correction.status_code == 201, correction.text
    resolved = client.post(
        f"/api/v1/conflicts/{unresolved[0]['id']}/resolve",
        headers=clinician,
        json={
            "resolution": "clinician_source_reconciled",
            "correction_entry_id": correction.json()["id"],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"

    owner_session.expire_all()
    stored_highlight = owner_session.get(Highlight, uuid.UUID(highlight["id"]))
    assert stored_highlight is not None
    assert stored_highlight.unresolved is False
    assert stored_highlight.critical is False
    assert "risk:critical" not in stored_highlight.feature_keys_json

    after = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert after.status_code == 200, after.text
    assert highlight["id"] not in {row["highlight_id"] for row in after.json()["cards"]}
    review_card = next(
        row
        for row in after.json()["review_cards"]
        if row["highlight_id"] == highlight["id"]
    )
    # Resolving the conflict removes the effective Critical/unresolved state,
    # but a clinician-confirmed item deliberately remains in the independent,
    # uncapped safety-review queue (hardening item 15).
    assert review_card["critical"] is False


def test_expired_calibration_is_requalified_on_explanation_and_glance_read(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    clinician = auth_headers("clinician")
    entry, highlight = _create_clinical_highlight(client, clinician)
    assessment = owner_session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.highlight_id == uuid.UUID(str(highlight["id"]))
        )
    ).one()
    parameters: dict[str, object] = {
        "schema": "clinical-fact-v2",
        "prompt": "fact-extraction-v2",
    }
    run = EvaluationRun(
        clinic_id=assessment.clinic_id,
        provider="fixture",
        exact_model_id="fixture-model",
        task="clinical_fact_extraction",
        request_parameters_json=parameters,
        dataset_manifest_sha256=evaluation_manifest_sha256(),
        code_commit=settings.NIGHTINGALE_SOURCE_COMMIT,
        calibration_split="fixture-calibration",
        holdout_split="fixture-holdout",
        total_sample_count=160,
        calibration_sample_count=40,
        holdout_sample_count=120,
        sample_count=120,
        status="completed",
    )
    owner_session.add(run)
    owner_session.flush()
    report = CalibrationReport(
        clinic_id=assessment.clinic_id,
        evaluation_run_id=run.id,
        provider=run.provider,
        exact_model_id=run.exact_model_id,
        task=run.task,
        request_parameters_sha256=request_parameters_sha256(parameters),
        dataset_manifest_sha256=run.dataset_manifest_sha256,
        code_commit=run.code_commit,
        total_sample_count=run.total_sample_count,
        calibration_sample_count=run.calibration_sample_count,
        holdout_sample_count=run.holdout_sample_count,
        sample_count=run.sample_count,
        consultation_count=20,
        confidence_band="high",
        accuracy_lower_bound=0.91,
        expires_at=get_datetime_utc() - timedelta(seconds=1),
    )
    owner_session.add(report)
    owner_session.flush()
    assessment.output_type = "extracted_fact"
    assessment.support_state = "supported"
    assessment.confidence_band = report.confidence_band
    assessment.confidence_lower_bound = report.accuracy_lower_bound
    assessment.calibration_report_id = report.id
    assessment.calibration_version = str(report.id)
    owner_session.add(assessment)
    owner_session.commit()

    explanation = client.get(
        f"/api/v1/highlights/{highlight['id']}/decision-explanation",
        headers=clinician,
    )
    assert explanation.status_code == 200, explanation.text
    assert explanation.json()["current_confidence_state"] == "review_required"
    assert (
        "CALIBRATION_REPORT_EXPIRED"
        in explanation.json()["confidence_qualification_reasons"]
    )

    accepted = client.post(
        f"/api/v1/highlights/{highlight['id']}/accept", headers=clinician
    )
    assert accepted.status_code == 200, accepted.text
    glance = client.get(
        f"/api/v1/patients/{entry['patient_id']}/glance", headers=clinician
    )
    assert glance.status_code == 200, glance.text
    assert highlight["id"] not in {
        row["highlight_id"] for row in glance.json()["cards"]
    }
    assert highlight["id"] in {
        row["highlight_id"] for row in glance.json()["review_cards"]
    }


def test_evaluator_persistence_uses_holdout_counts_and_replaces_expired_report(
    owner_session: Session,
) -> None:
    user = owner_session.get(User, demo_id("user-clinician"))
    membership = owner_session.get(ClinicMembership, demo_id("membership-clinician"))
    assert user is not None and membership is not None
    context = RequestContext(user=user, membership=membership)
    parameters: dict[str, object] = {"schema": "fixture-v1"}
    kwargs = {
        "context": context,
        "provider": "fixture",
        "model": "fixture-model",
        "task": "clinical_fact_extraction",
        "request_parameters": parameters,
        "manifest_sha256": "a" * 64,
        "code_commit": "b" * 40,
        "calibration_ids": [f"cal-{index}" for index in range(20)],
        "holdout_ids": [f"holdout-{index}" for index in range(20)],
        "calibration_outcomes": [True] * 20,
        "holdout_outcomes": [True] * 120,
        "metrics": {"accuracy": 1.0},
    }
    first = _persist_report(owner_session, **kwargs)
    run = owner_session.get(EvaluationRun, first.evaluation_run_id)
    assert run is not None
    assert run.sample_count == first.sample_count == 120
    assert run.holdout_sample_count == first.holdout_sample_count == 120
    assert run.calibration_sample_count == first.calibration_sample_count == 20
    assert run.total_sample_count == first.total_sample_count == 140
    qualified = qualify_calibration_report(
        owner_session,
        first,
        provider="fixture",
        exact_model_id="fixture-model",
        task="clinical_fact_extraction",
        request_parameters=parameters,
        dataset_manifest_sha256="a" * 64,
        code_commit="b" * 40,
    )
    assert qualified.qualified is True, qualified.reasons

    first.expires_at = get_datetime_utc() - timedelta(seconds=1)
    owner_session.add(first)
    owner_session.commit()
    replacement = _persist_report(owner_session, **kwargs)
    assert replacement.id != first.id
    assert replacement.expires_at > get_datetime_utc()


def test_source_edit_requires_support_review_while_original_pointer_resolves(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
) -> None:
    clinician = auth_headers("clinician")
    entry, highlight = _create_clinical_highlight(client, clinician)
    accepted = client.post(
        f"/api/v1/highlights/{highlight['id']}/accept", headers=clinician
    )
    assert accepted.status_code == 200, accepted.text
    edited = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=clinician | {"If-Match": str(entry["version_id"])},
        json={"content": "The current source wording has changed."},
    )
    assert edited.status_code == 200, edited.text
    edited_again = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=clinician | {"If-Match": edited.json()["version_id"]},
        json={"content": "The current source wording changed again."},
    )
    assert edited_again.status_code == 200, edited_again.text

    reviews = client.get(
        f"/api/v1/patients/{entry['patient_id']}/highlight-support-reviews",
        headers=clinician,
    )
    assert reviews.status_code == 200, reviews.text
    highlight_reviews = [
        row for row in reviews.json() if row["highlight_id"] == highlight["id"]
    ]
    assert sum(row["review_status"] == "pending" for row in highlight_reviews) == 1
    assert sum(row["review_status"] == "superseded" for row in highlight_reviews) == 1
    review = next(row for row in highlight_reviews if row["review_status"] == "pending")
    assert review["review_status"] == "pending"
    assert review["support_state"] == "historical"
    assert review["source_entry_version_id"] == entry["version_id"]
    assert review["observed_current_version_id"] == edited_again.json()["version_id"]

    glance = client.get(
        f"/api/v1/patients/{entry['patient_id']}/glance", headers=clinician
    )
    assert glance.status_code == 200, glance.text
    assert highlight["id"] not in {
        row["highlight_id"] for row in glance.json()["cards"]
    }
    historical = next(
        row
        for row in glance.json()["review_cards"]
        if row["highlight_id"] == highlight["id"]
    )
    assert historical["support_state"] == "historical"

    resolved = client.get(
        f"/api/v1/provenance/{highlight['provenance_pointer_id']}/resolve",
        headers=clinician,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["entry_version_id"] == entry["version_id"]
    assert resolved.json()["exact_quote"] == "IMPORTANT"
    assert resolved.json()["support_state"] == "historical"

    reaffirmed = client.post(
        f"/api/v1/highlights/{highlight['id']}/support-review/reaffirm",
        headers=clinician,
    )
    assert reaffirmed.status_code == 200, reaffirmed.text
    assert reaffirmed.json()["support_review_required"] is False
    assert reaffirmed.json()["current_priority_eligible"] is True
    after = client.get(
        f"/api/v1/patients/{entry['patient_id']}/glance", headers=clinician
    )
    assert after.status_code == 200, after.text
    assert highlight["id"] not in {row["highlight_id"] for row in after.json()["cards"]}
    assert highlight["id"] in {
        row["highlight_id"] for row in after.json()["review_cards"]
    }


def test_patient_owned_source_edit_also_invalidates_clinical_highlight_support(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
) -> None:
    patient = auth_headers("patient")
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=patient).json()["data"][0]["id"]
    content = "prefix IMPORTANT suffix"
    entry_response = client.post(
        "/api/v1/entries",
        headers=patient,
        json={
            "patient_id": patient_id,
            "section": "patient",
            "title": "Patient-owned symptom note",
            "content": content,
            "patient_facing": True,
        },
    )
    assert entry_response.status_code == 201, entry_response.text
    entry = entry_response.json()
    highlight_response = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=clinician,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Patient-reported current priority",
            "patient_facing": False,
        },
    )
    assert highlight_response.status_code == 201, highlight_response.text
    highlight = highlight_response.json()
    accepted = client.post(
        f"/api/v1/highlights/{highlight['id']}/accept", headers=clinician
    )
    assert accepted.status_code == 200, accepted.text

    edited = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=patient | {"If-Match": str(entry["version_id"])},
        json={"content": "The patient has revised the source wording."},
    )
    assert edited.status_code == 200, edited.text

    reviews = client.get(
        f"/api/v1/patients/{patient_id}/highlight-support-reviews",
        headers=clinician,
    )
    assert reviews.status_code == 200, reviews.text
    review = next(
        row for row in reviews.json() if row["highlight_id"] == highlight["id"]
    )
    assert review["review_status"] == "pending"
    assert review["support_state"] == "historical"
    assert review["source_entry_version_id"] == entry["version_id"]
    assert review["observed_current_version_id"] == edited.json()["version_id"]

    glance = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert glance.status_code == 200, glance.text
    assert highlight["id"] not in {
        row["highlight_id"] for row in glance.json()["cards"]
    }
    historical = next(
        row
        for row in glance.json()["review_cards"]
        if row["highlight_id"] == highlight["id"]
    )
    assert historical["support_state"] == "historical"
    assert historical["support_review_required"] is True
    assert historical["current_priority_eligible"] is False

    provenance = client.get(
        f"/api/v1/provenance/{highlight['provenance_pointer_id']}/resolve",
        headers=clinician,
    )
    assert provenance.status_code == 200, provenance.text
    assert provenance.json()["entry_version_id"] == entry["version_id"]
    assert provenance.json()["exact_quote"] == "IMPORTANT"
    assert provenance.json()["support_state"] == "historical"


def _medication_attestation(
    owner_session: Session,
    *,
    source_entry_version_id: str,
    dose_value: float,
    frequency: str,
) -> dict[str, object]:
    assertion = owner_session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.source_entry_version_id
            == uuid.UUID(source_entry_version_id),
            ClinicalFactAssertion.assertion_state == "active",
            col(ClinicalFactAssertion.medication_ciphertext).is_not(None),
        )
    ).first()
    assert assertion is not None
    return {
        "assertion_id": str(assertion.id),
        "medication": "metformin",
        "dose_value": dose_value,
        "dose_unit": "mg",
        "route": "oral",
        "frequency": frequency,
        "confirmed": True,
    }


def test_medication_gate_and_correction_survive_delivery_failure_then_acknowledge(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinician = auth_headers("clinician")
    patient = auth_headers("patient")
    patient_id = client.get("/api/v1/patients", headers=patient).json()["data"][0]["id"]
    entry_response = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Medication summary",
            "content": "Started metformin 500 mg PO BID.",
            "patient_facing": False,
        },
    )
    assert entry_response.status_code == 201, entry_response.text
    entry = entry_response.json()

    blocked = client.post(
        f"/api/v1/entries/{entry['id']}/patient-publications",
        headers=clinician,
        json={"entry_version_id": entry["version_id"]},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "MEDICATION_REVIEW_REQUIRED"

    first_review = _medication_attestation(
        owner_session,
        source_entry_version_id=entry["version_id"],
        dose_value=500,
        frequency="twice daily",
    )
    published = client.post(
        f"/api/v1/entries/{entry['id']}/patient-publications",
        headers=clinician,
        json={
            "entry_version_id": entry["version_id"],
            "medication_reviews": [first_review],
        },
    )
    assert published.status_code == 201, published.text
    old_publication = published.json()
    assert old_publication["medication_review_complete"] is True

    revised = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=clinician | {"If-Match": old_publication["entry_version_id"]},
        json={"content": "Continue metformin 1000 mg PO daily."},
    )
    assert revised.status_code == 200, revised.text
    second_review = _medication_attestation(
        owner_session,
        source_entry_version_id=revised.json()["version_id"],
        dose_value=1000,
        frequency="once daily",
    )

    def delivery_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("deterministic provider outage")

    monkeypatch.setattr("app.api.routes.trust.dispatch_notification", delivery_failure)
    correction_key = "correction-idempotency-fixture"
    correction_body = {
        "replacement_entry_version_id": revised.json()["version_id"],
        "medication_reviews": [second_review],
    }
    suppressed_outreach = client.post(
        f"/api/v1/patient-publications/{old_publication['id']}/correct",
        headers=clinician | {"Idempotency-Key": "suppressed-outreach-fixture"},
        json=correction_body | {"outreach_required": False},
    )
    assert suppressed_outreach.status_code == 422, suppressed_outreach.text
    corrected = client.post(
        f"/api/v1/patient-publications/{old_publication['id']}/correct",
        headers=clinician | {"Idempotency-Key": correction_key},
        json=correction_body,
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["notification_id"] is not None
    assert corrected.json()["notification_state"] == "failed"
    assert corrected.json()["delivery_warning"] == "notification_delivery_failed"
    identical_retry = client.post(
        f"/api/v1/patient-publications/{old_publication['id']}/correct",
        headers=clinician | {"Idempotency-Key": correction_key},
        json=correction_body,
    )
    assert identical_retry.status_code == 201, identical_retry.text
    assert identical_retry.json()["id"] == corrected.json()["id"]
    assert (
        identical_retry.json()["notification_id"] == corrected.json()["notification_id"]
    )
    assert identical_retry.json()["notification_state"] == "failed"
    assert identical_retry.json()["delivery_warning"] == "notification_delivery_failed"
    same_key_different_body = client.post(
        f"/api/v1/patient-publications/{old_publication['id']}/correct",
        headers=clinician | {"Idempotency-Key": correction_key},
        json=correction_body | {"medication_reviews": []},
    )
    assert same_key_different_body.status_code == 409, same_key_different_body.text
    assert (
        same_key_different_body.json()["detail"]["code"]
        == "PUBLICATION_CORRECTION_IDEMPOTENCY_CONFLICT"
    )
    conflicting_retry = client.post(
        f"/api/v1/patient-publications/{old_publication['id']}/correct",
        headers=clinician | {"Idempotency-Key": "different-correction-key"},
        json=correction_body,
    )
    assert conflicting_retry.status_code == 409, conflicting_retry.text
    assert (
        conflicting_retry.json()["detail"]["code"]
        == "PUBLICATION_CORRECTION_ALREADY_REPLACED"
    )
    owner_session.expire_all()
    old_stored = owner_session.get(PatientPublication, uuid.UUID(old_publication["id"]))
    assert old_stored is not None and old_stored.withdrawn_at is not None
    replacement = owner_session.exec(
        select(PatientPublication).where(
            PatientPublication.supersedes_publication_id == old_stored.id
        )
    ).one()
    assert replacement.withdrawn_at is None
    expected_key_sha256 = hashlib.sha256(correction_key.encode()).hexdigest()
    normalized_request = PatientPublicationCorrectionCreate.model_validate(
        correction_body
    ).model_dump(mode="json")
    expected_request_sha256 = hashlib.sha256(
        json.dumps(
            {
                "publication_id": old_publication["id"],
                "body": normalized_request,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert replacement.correction_idempotency_key_sha256 == expected_key_sha256
    assert replacement.correction_request_sha256 == expected_request_sha256
    assert replacement.medication_review_complete is True
    assert len(replacement.medication_review_json) == 1
    stored_review = replacement.medication_review_json[0]
    for key, value in second_review.items():
        if key != "assertion_id":
            assert stored_review[key] == value
    assert stored_review["formulary_qualification_source"] == "deployment_fixture"
    assert stored_review["formulary_version"] == "nightingale-clinic-formulary-v1"
    assert stored_review["assertion_id"] != second_review["assertion_id"]
    published_assertion = owner_session.get(
        ClinicalFactAssertion, uuid.UUID(str(stored_review["assertion_id"]))
    )
    assert published_assertion is not None
    assert published_assertion.source_entry_version_id == replacement.entry_version_id
    assert published_assertion.assertion_state == "active"
    queued = owner_session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.publication_id == replacement.id,
            NotificationOutbox.purpose == "correction",
        )
    ).one()
    assert queued.state == "failed"
    assert queued.id == uuid.UUID(corrected.json()["notification_id"])
    outreach = owner_session.exec(
        select(PublicationCorrectionOutreach).where(
            PublicationCorrectionOutreach.replacement_publication_id == replacement.id
        )
    ).one()
    assert outreach.status == "pending"
    invalidation = owner_session.exec(
        select(PatientPortalEvent).where(
            PatientPortalEvent.aggregate_id == old_stored.id,
            PatientPortalEvent.event_type == "patient_publication.corrected",
        )
    ).one()
    assert invalidation.patient_id == uuid.UUID(patient_id)

    events = client.get(f"/api/v1/patients/{patient_id}/portal-events", headers=patient)
    assert events.status_code == 200, events.text
    assert any(
        item["event_type"] == "patient_publication.corrected"
        and item["aggregate_id"] == old_publication["id"]
        for item in events.json()
    )
    timeline = client.get(f"/api/v1/patients/{patient_id}/timeline", headers=patient)
    assert timeline.status_code == 200, timeline.text
    corrected_entry = next(
        item for item in timeline.json()["data"] if item["id"] == entry["id"]
    )
    assert corrected_entry["content"] == "Continue metformin 1000 mg PO daily."
    receipts = client.get(
        f"/api/v1/patients/{patient_id}/publication-receipts", headers=patient
    )
    assert receipts.status_code == 200, receipts.text
    corrected_receipt = next(
        item
        for item in receipts.json()
        if item["publication_id"] == str(replacement.id)
    )
    assert corrected_receipt["acknowledgement_state"] == "pending"
    assert corrected_receipt["outreach_required"] is True

    acknowledged = client.post(
        f"/api/v1/patient-publications/{replacement.id}/acknowledgements",
        headers=patient,
        json={"event_type": "acknowledged"},
    )
    assert acknowledged.status_code == 201, acknowledged.text
    owner_session.expire_all()
    completed_outreach = owner_session.get(PublicationCorrectionOutreach, outreach.id)
    assert completed_outreach is not None
    assert completed_outreach.status == "acknowledged"
    assert completed_outreach.completed_at is not None

    duplicate = PatientPublication(
        clinic_id=replacement.clinic_id,
        patient_id=replacement.patient_id,
        entry_id=replacement.entry_id,
        entry_version_id=replacement.entry_version_id,
        supersedes_publication_id=old_stored.id,
        approved_by_membership_id=replacement.approved_by_membership_id,
        correction_idempotency_key_sha256=expected_key_sha256,
        correction_request_sha256=hashlib.sha256(b"other-request").hexdigest(),
        withdrawn_at=get_datetime_utc(),
    )
    with pytest.raises(IntegrityError):
        with owner_session.begin_nested():
            owner_session.add(duplicate)
            owner_session.flush()

    invalid_hash_pairs = (
        (None, hashlib.sha256(b"request-without-key").hexdigest()),
        (hashlib.sha256(b"key-without-request").hexdigest(), None),
        ("not-a-sha256", hashlib.sha256(b"valid-request").hexdigest()),
        (hashlib.sha256(b"valid-key").hexdigest(), "not-a-sha256"),
    )
    for invalid_key_hash, invalid_request_hash in invalid_hash_pairs:
        invalid = PatientPublication(
            clinic_id=replacement.clinic_id,
            patient_id=replacement.patient_id,
            entry_id=replacement.entry_id,
            entry_version_id=replacement.entry_version_id,
            supersedes_publication_id=old_stored.id,
            approved_by_membership_id=replacement.approved_by_membership_id,
            correction_idempotency_key_sha256=invalid_key_hash,
            correction_request_sha256=invalid_request_hash,
            withdrawn_at=get_datetime_utc(),
        )
        with pytest.raises(
            IntegrityError,
            match="ck_patient_publication_correction_hashes",
        ):
            with owner_session.begin_nested():
                owner_session.add(invalid)
                owner_session.flush()


def test_correction_queue_failure_returns_durable_warning_and_replays(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    created = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Patient summary",
            "content": "Follow the reviewed care plan.",
            "patient_facing": False,
        },
    )
    assert created.status_code == 201, created.text
    published = client.post(
        f"/api/v1/entries/{created.json()['id']}/patient-publications",
        headers=clinician,
        json={"entry_version_id": created.json()["version_id"]},
    )
    assert published.status_code == 201, published.text
    revised = client.patch(
        f"/api/v1/entries/{created.json()['id']}",
        headers=clinician | {"If-Match": published.json()["entry_version_id"]},
        json={"content": "Follow the corrected and reviewed care plan."},
    )
    assert revised.status_code == 200, revised.text

    def queue_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("deterministic queue outage")

    monkeypatch.setattr("app.api.routes.trust.queue_notification", queue_failure)
    correction_key = "correction-queue-failure-fixture"
    body = {"replacement_entry_version_id": revised.json()["version_id"]}
    corrected = client.post(
        f"/api/v1/patient-publications/{published.json()['id']}/correct",
        headers=clinician | {"Idempotency-Key": correction_key},
        json=body,
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["notification_id"] is None
    assert corrected.json()["notification_state"] is None
    assert corrected.json()["delivery_warning"] == "notification_queue_failed"
    assert corrected.json()["outreach_required"] is True

    replay = client.post(
        f"/api/v1/patient-publications/{published.json()['id']}/correct",
        headers=clinician | {"Idempotency-Key": correction_key},
        json=body,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == corrected.json()["id"]
    assert replay.json()["delivery_warning"] == "notification_queue_failed"

    owner_session.expire_all()
    replacement = owner_session.get(
        PatientPublication, uuid.UUID(corrected.json()["id"])
    )
    assert replacement is not None
    assert (
        replacement.correction_idempotency_key_sha256
        == hashlib.sha256(correction_key.encode()).hexdigest()
    )
    outreach = owner_session.exec(
        select(PublicationCorrectionOutreach).where(
            PublicationCorrectionOutreach.replacement_publication_id == replacement.id
        )
    ).one()
    assert outreach.status == "pending"
    assert outreach.notification_id is None


def test_pending_safety_review_queue_is_uncapped_and_exposure_set_is_complete(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    content = "prefix IMPORTANT suffix"
    entry_response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Uncapped safety review fixture",
            "content": content,
            "patient_facing": False,
        },
    )
    assert entry_response.status_code == 201, entry_response.text
    entry = entry_response.json()

    created_ids: set[str] = set()
    for index in range(7):
        created = client.post(
            f"/api/v1/entries/{entry['id']}/highlights",
            headers=headers,
            json={
                "entry_version_id": entry["version_id"],
                "start_offset": 7,
                "end_offset": 16,
                "exact_quote": "IMPORTANT",
                "prefix": "prefix ",
                "suffix": " suffix",
                "label": f"Pending safety review {index}",
                "critical": True,
                "unresolved": True,
                "patient_facing": False,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "pending"
        created_ids.add(created.json()["id"])

    staff_headers = auth_headers("staff")
    ordinary_entry_response = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Ordinary priority fixture",
            "content": content,
            "patient_facing": False,
        },
    )
    assert ordinary_entry_response.status_code == 201, ordinary_entry_response.text
    ordinary_entry = ordinary_entry_response.json()
    ordinary_ids: set[str] = set()
    for index in range(7):
        created = client.post(
            f"/api/v1/entries/{ordinary_entry['id']}/highlights",
            headers=staff_headers,
            json={
                "entry_version_id": ordinary_entry["version_id"],
                "start_offset": 7,
                "end_offset": 16,
                "exact_quote": "IMPORTANT",
                "prefix": "prefix ",
                "suffix": " suffix",
                "label": f"Ordinary priority {index}",
                "patient_facing": False,
            },
        )
        assert created.status_code == 201, created.text
        accepted = client.post(
            f"/api/v1/highlights/{created.json()['id']}/accept",
            headers=staff_headers,
        )
        assert accepted.status_code == 200, accepted.text
        ordinary_ids.add(created.json()["id"])

    glance = client.get(f"/api/v1/patients/{patient_id}/glance", headers=headers)
    assert glance.status_code == 200, glance.text
    payload = glance.json()
    card_ids = {item["highlight_id"] for item in payload["cards"]}
    assert len(card_ids) == 5
    assert card_ids <= ordinary_ids
    review_ids = {item["highlight_id"] for item in payload["review_cards"]}
    assert created_ids <= review_ids
    assert not (card_ids & review_ids)

    owner_session.expire_all()
    latest = owner_session.exec(
        select(ImportanceCandidateExposure)
        .where(ImportanceCandidateExposure.patient_id == uuid.UUID(patient_id))
        .order_by(desc(col(ImportanceCandidateExposure.observed_at)))
    ).first()
    assert latest is not None
    exposures = owner_session.exec(
        select(ImportanceCandidateExposure).where(
            ImportanceCandidateExposure.candidate_set_id == latest.candidate_set_id
        )
    ).all()
    assert {str(item.highlight_id) for item in exposures} == created_ids | ordinary_ids
    protected_exposures = [item for item in exposures if item.protected]
    ordinary_exposures = [item for item in exposures if not item.protected]
    assert {str(item.highlight_id) for item in protected_exposures} == created_ids
    assert all(item.displayed for item in protected_exposures)
    assert all(item.surface == "clinical_review" for item in protected_exposures)
    assert sorted(item.rank for item in protected_exposures) == list(range(1, 8))
    assert {str(item.highlight_id) for item in ordinary_exposures} == ordinary_ids
    assert sum(item.displayed for item in ordinary_exposures) == 5
    assert all(item.surface == "current_priorities" for item in ordinary_exposures)
    assert sorted(item.rank for item in ordinary_exposures) == list(range(1, 8))
