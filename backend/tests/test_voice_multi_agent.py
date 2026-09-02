from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace

from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine
from app.models import ClinicalFact, ConflictCase
from app.services.voice.multi_agent import (
    consult_agent_payload,
    consult_fact_candidates,
    consult_warning_codes,
    run_consult_on_segments,
)
from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
)
from app.services.voice.providers.deterministic import SyntheticFixtureProvider
from tests.test_voice_worker import _create_recording, _run_job

_CLINICIAN = "We'll continue metformin 500 mg twice daily."
_PATIENT = "我对盘尼西林不过敏，是胃不舒服。"
_FAMILY = "Dia ada alahan kepada penicillin masa kecil."


def _consult_segments() -> list[TranscriptSegmentResult]:
    cursor = 0
    segments: list[TranscriptSegmentResult] = []
    rows = (
        (_CLINICIAN, "SPEAKER_00", "en", 0, 3_000, None),
        (_PATIENT, "SPEAKER_01", "zh", 3_100, 6_500, None),
        (_FAMILY, "SPEAKER_02", "ms", 6_600, 10_000, None),
    )
    for text, speaker, language, start_ms, end_ms, overlap in rows:
        segments.append(
            TranscriptSegmentResult(
                text=text,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_id=speaker,
                speaker_ids=(speaker,),
                detected_language=language,
                source_language=language,
                language_confidence=0.92,
                confidence=0.92,
                confidence_source="synthetic_fixture",
                overlap_group_id=overlap,
                text_start=cursor,
                text_end=cursor + len(text),
            )
        )
        cursor += len(text) + 1
    return segments


def _enable_consult_fixture(monkeypatch: MonkeyPatch) -> str:
    segments = _consult_segments()
    transcript = "\n".join(item.text for item in segments)

    def consult_fixture(
        _provider: SyntheticFixtureProvider, fixture_id: str
    ) -> TranscriptResult:
        assert fixture_id == "code-switch-overlap-v1"
        return TranscriptResult(
            text=transcript,
            segments=[
                replace(item, text_start=None, text_end=None) for item in segments
            ],
            provider="deterministic-synthetic-fixture",
            model=fixture_id,
            detected_language="multilingual",
            warnings=("SYNTHETIC_FIXTURE",),
        )

    monkeypatch.setattr(settings, "VOICE_MULTI_AGENT_PIPELINE", True)
    monkeypatch.setattr(SyntheticFixtureProvider, "transcribe_fixture", consult_fixture)
    return transcript


def test_voice_multi_agent_pipeline_defaults_off() -> None:
    assert settings.VOICE_MULTI_AGENT_PIPELINE is False


def test_consult_agents_propose_roles_facts_and_block_publish() -> None:
    segments = _consult_segments()
    state = run_consult_on_segments(segments, consult_id="consult-01-adapter")
    assert state.speaker_roles == {
        "SPEAKER_00": "clinician",
        "SPEAKER_01": "patient",
        "SPEAKER_02": "family",
    }
    assert state.publish_blocked is True
    assert "UNRESOLVED_ALLERGY_CONFLICT" in state.warning_codes
    candidates, extra = consult_fact_candidates(state, segments)
    assert extra == []
    keys = {
        (fact.fact_type, fact.key, fact.value, fact.review_required)
        for fact, _, _, _ in candidates
    }
    assert ("allergy", "penicillin", "absent", False) in keys
    assert ("allergy", "penicillin", "present", True) in keys
    assert ("dose", "metformin", "500mg", False) in keys
    payload = consult_agent_payload(state)
    assert payload["speaker_roles"]["SPEAKER_02"] == "family"
    assert payload["conflicts"][0]["key"] == "penicillin"
    assert payload["conflicts"][0]["auto_resolved"] is False
    assert "MULTI_AGENT_CONSULT_PROPOSAL" in consult_warning_codes(state)
    assert "PUBLISH_BLOCKED" in consult_warning_codes(state)
    for fact, start, end, segment in candidates:
        assert segment.text[fact.start : fact.end] == fact.quote
        assert start == (segment.text_start or 0) + fact.start
        assert end == (segment.text_start or 0) + fact.end


def test_quote_mismatch_is_dropped_fail_closed() -> None:
    segments = _consult_segments()
    state = run_consult_on_segments(segments, consult_id="mismatch")
    broken = [
        replace(segment, text="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        if segment.speaker_id == "SPEAKER_02"
        else segment
        for segment in segments
    ]
    _candidates, extra = consult_fact_candidates(state, broken)
    assert "AGENT_FACT_QUOTE_MISMATCH" in extra


def test_worker_flag_persists_proposed_facts_roles_and_conflicts(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    monkeypatch: MonkeyPatch,
) -> None:
    _enable_consult_fixture(monkeypatch)
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    _run_job(job_id)

    status = client.get(f"/api/v1/voice/sessions/{session_id}", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["state"] == "needs_review"

    transcript_response = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=headers
    )
    assert transcript_response.status_code == 200, transcript_response.text
    body = transcript_response.json()
    assert body["needs_review"] is True
    assert "MULTI_AGENT_CONSULT_PROPOSAL" in body["warning_codes"]
    assert "UNRESOLVED_ALLERGY_CONFLICT" in body["warning_codes"]
    assert "PUBLISH_BLOCKED" in body["warning_codes"]
    assert body["consult_agent"]["enabled"] is True
    assert body["consult_agent"]["speaker_roles"] == {
        "SPEAKER_00": "clinician",
        "SPEAKER_01": "patient",
        "SPEAKER_02": "family",
    }
    assert body["consult_agent"]["conflicts"][0]["key"] == "penicillin"
    assert body["consult_agent"]["conflicts"][0]["auto_resolved"] is False
    values = {item["value"] for item in body["facts"]}
    assert "penicillin allergy:absent" in values
    assert "penicillin allergy:present" in values
    assert "500mg" in values
    roles = {item["speaker_role"] for item in body["facts"]}
    assert {"clinician", "patient", "family"} <= roles
    assert all(item["status"] == "proposed" for item in body["facts"])

    with Session(engine) as db:
        rows = db.exec(
            select(ClinicalFact).where(ClinicalFact.session_id == uuid.UUID(session_id))
        ).all()
        assert rows
        assert all(row.status == "proposed" for row in rows)
        assert all(row.reviewed_by_id is None for row in rows)
        assert (
            db.exec(
                select(ConflictCase).where(
                    ConflictCase.clinic_id == rows[0].clinic_id
                )
            ).all()
            == []
        )


def test_publish_stays_gated_so_agents_do_not_create_conflict_cases(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    monkeypatch: MonkeyPatch,
) -> None:
    _enable_consult_fixture(monkeypatch)
    headers = auth_headers("clinician")
    _patient_id, session_id, job_id = _create_recording(
        client, headers, synthetic_fixture=True
    )
    _run_job(job_id)
    transcript = client.get(
        f"/api/v1/voice/sessions/{session_id}/transcript", headers=headers
    )
    assert transcript.status_code == 200, transcript.text
    published = client.post(
        f"/api/v1/voice/sessions/{session_id}/publish",
        headers=headers,
        json={"expected_revision_id": transcript.json()["id"]},
    )
    # Metformin is proposed without a complete four-axis regimen. The product
    # must not invent a ConflictCase to get past that human gate.
    assert published.status_code == 409, published.text
    assert published.json()["detail"]["code"] in {
        "VOICE_MEDICATION_REVIEW_REQUIRED",
        "VOICE_MEDICATION_REGIMEN_INCOMPLETE",
        "VOICE_MEDICATION_REVIEW_MISMATCH",
    }
    with Session(engine) as db:
        assert db.exec(select(ConflictCase)).all() == []
        facts = db.exec(select(ClinicalFact)).all()
        assert facts
        assert all(row.status == "proposed" for row in facts)
