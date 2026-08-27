import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import (
    CalibrationReport,
    DecisionAssessment,
    EvaluationRun,
    ImportanceFeedbackEvent,
    ImportanceImpression,
    PatientPublication,
    PatientPublicationItem,
    ProvenancePointer,
    get_datetime_utc,
)
from app.seed import demo_id
from app.services.conflicts import extract_normalized_facts
from app.services.decisioning import (
    deterministic_risk,
    matching_calibration_report,
    request_parameters_sha256,
)
from app.services.redaction import RedactionService
from app.services.voice.providers.base import TranscriptSegmentResult
from app.services.voice.worker import _apply_calibration


def _create_highlight(
    client: TestClient,
    headers: dict[str, str],
    *,
    content: str = "Patient has a severe penicillin allergy.",
) -> tuple[dict, dict]:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Source-bound finding",
            "content": content,
            "patient_facing": False,
        },
    )
    assert entry.status_code == 201, entry.text
    highlight = client.post(
        f"/api/v1/entries/{entry.json()['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry.json()["version_id"],
            "start_offset": 0,
            "end_offset": len(content),
            "exact_quote": content,
            "label": "Severe penicillin allergy",
            "feature_keys": ["entity:allergy"],
        },
    )
    assert highlight.status_code == 201, highlight.text
    return entry.json(), highlight.json()


def _make_assessment_abstain(owner_session: Session, highlight_id: str) -> None:
    assessment = owner_session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.highlight_id == uuid.UUID(highlight_id)
        )
    ).one()
    assessment.support_state = "unsupported"
    assessment.confidence_band = "unavailable"
    assessment.abstained = True
    assessment.abstention_reason = "CALIBRATION_UNAVAILABLE"
    owner_session.add(assessment)
    owner_session.commit()


def test_abstained_highlight_never_enters_ready_glance(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    headers = auth_headers("clinician")
    entry, highlight = _create_highlight(client, headers)
    _make_assessment_abstain(owner_session, highlight["id"])
    accepted = client.post(
        f"/api/v1/highlights/{highlight['id']}/accept", headers=headers
    )
    assert accepted.status_code == 200, accepted.text
    glance = client.get(
        f"/api/v1/patients/{entry['patient_id']}/glance", headers=headers
    ).json()
    assert highlight["id"] not in {item["highlight_id"] for item in glance["cards"]}
    assert highlight["id"] in {item["highlight_id"] for item in glance["review_cards"]}


def test_critical_abstention_remains_visible_for_review(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    headers = auth_headers("clinician")
    entry, highlight = _create_highlight(
        client, headers, content="Anaphylaxis after penicillin exposure."
    )
    _make_assessment_abstain(owner_session, highlight["id"])
    accepted = client.post(
        f"/api/v1/highlights/{highlight['id']}/accept", headers=headers
    )
    assert accepted.status_code == 200
    glance = client.get(
        f"/api/v1/patients/{entry['patient_id']}/glance", headers=headers
    ).json()
    review = next(
        item
        for item in glance["review_cards"]
        if item["highlight_id"] == highlight["id"]
    )
    assert review["critical"] is True
    assert review["review_state"] == "abstained"


@pytest.mark.unit
def test_model_cannot_lower_deterministic_risk_floor() -> None:
    decision = deterministic_risk(
        fact_type="allergy",
        text="Severe allergy with anaphylaxis",
        model_risk="standard",
    )
    assert decision.deterministic_floor == "critical"
    assert decision.effective_risk == "critical"
    assert "ANAPHYLAXIS" in decision.rule_ids


@pytest.mark.unit
def test_medication_dose_route_frequency_conflicts() -> None:
    left = extract_normalized_facts("Continue metformin 1 g PO BID.")
    right = extract_normalized_facts("Stopped metformin. Metformin 500 mg IV daily.")
    left_values = {(item.fact_type, item.key, item.value) for item in left}
    right_values = {(item.fact_type, item.key, item.value) for item in right}
    assert ("dose", "metformin", "1000mg") in left_values
    assert ("route", "metformin", "oral") in left_values
    assert ("frequency", "metformin", "twice_daily") in left_values
    assert ("medication", "metformin", "stopped") in right_values
    assert ("dose", "metformin", "500mg") in right_values
    assert ("route", "metformin", "intravenous") in right_values


@pytest.mark.unit
def test_human_ai_voice_conflicts() -> None:
    origins = {"human", "ai", "voice"}
    facts = {
        origin: extract_normalized_facts("Metformin 500 mg PO BID.")
        for origin in origins
    }
    assert all(
        (item.fact_type, item.value)
        in {("dose", "500mg"), ("route", "oral"), ("frequency", "twice_daily")}
        for rows in facts.values()
        for item in rows
    )
    assert set(facts) == origins


def test_calibration_report_model_and_hash_match(owner_session: Session) -> None:
    clinic_id = demo_id("clinic-primary")
    parameters = {"response_format": "diarized_json", "chunking_strategy": "auto"}
    run = EvaluationRun(
        clinic_id=clinic_id,
        provider="openai",
        exact_model_id="gpt-4o-transcribe-diarize",
        task="voice_transcription",
        request_parameters_json=parameters,
        dataset_manifest_sha256="a" * 64,
        code_commit="test",
        calibration_split="40 consultations",
        holdout_split="17 consultations",
        sample_count=120,
        status="completed",
    )
    owner_session.add(run)
    owner_session.flush()
    report = CalibrationReport(
        clinic_id=clinic_id,
        evaluation_run_id=run.id,
        provider="openai",
        exact_model_id="gpt-4o-transcribe-diarize",
        task="voice_transcription",
        request_parameters_sha256=request_parameters_sha256(parameters),
        dataset_manifest_sha256="a" * 64,
        code_commit="test",
        sample_count=120,
        consultation_count=17,
        confidence_band="medium",
        accuracy_lower_bound=0.9,
        metrics_json={"wer": 0.08},
        expires_at=get_datetime_utc() + timedelta(days=30),
    )
    owner_session.add(report)
    owner_session.commit()
    assert (
        matching_calibration_report(
            owner_session,
            clinic_id=clinic_id,
            provider="openai",
            exact_model_id="gpt-4o-transcribe-diarize",
            task="voice_transcription",
            request_parameters=parameters,
            dataset_manifest_sha256="a" * 64,
            code_commit="test",
        )
        is not None
    )
    assert (
        matching_calibration_report(
            owner_session,
            clinic_id=clinic_id,
            provider="openai",
            exact_model_id="changed-model",
            task="voice_transcription",
            request_parameters=parameters,
            dataset_manifest_sha256="a" * 64,
            code_commit="test",
        )
        is None
    )
    assert (
        matching_calibration_report(
            owner_session,
            clinic_id=clinic_id,
            provider="openai",
            exact_model_id="gpt-4o-transcribe-diarize",
            task="voice_transcription",
            request_parameters=parameters,
            dataset_manifest_sha256="a" * 64,
            code_commit="changed-code",
        )
        is None
    )


@pytest.mark.unit
def test_provider_confidence_is_not_used_directly() -> None:
    provider_segment = TranscriptSegmentResult(
        text="metformin",
        start_ms=0,
        end_ms=500,
        speaker_id="doctor",
        detected_language="en",
        confidence=0.99,
        confidence_source="provider",
        overlap_group_id=None,
    )
    result = _apply_calibration([provider_segment], None)
    assert result[0].confidence is None
    assert result[0].confidence_source == "unavailable"


@pytest.mark.unit
def test_redaction_recall_and_clinical_span_preservation(tmp_path: Path) -> None:
    service = RedactionService(require_presidio=False)
    clinical = "penicillin allergy; metformin 500 mg PO BID; severe rash"
    text = (
        "Patient Alice Tan; ID S1234567D; MRN-2026-00001; phone +65 9123 4567; "
        f"email alice@example.com; {clinical}."
    )
    result = service.redact(
        text,
        clinic_id=uuid.uuid4(),
        record_id=uuid.uuid4(),
        known_names=["Alice Tan"],
    )
    assert all(
        value not in result.redacted_text
        for value in (
            "Alice Tan",
            "S1234567D",
            "MRN-2026-00001",
            "+65 9123 4567",
            "alice@example.com",
        )
    )
    assert clinical in result.redacted_text
    assert not list(tmp_path.iterdir())


def test_importance_exposure_dedup_and_feedback_reasons(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    headers = auth_headers("clinician")
    _, highlight = _create_highlight(client, headers, content="Follow-up in one week.")
    body = {
        "highlight_id": highlight["id"],
        "view_event_id": "priority:stable-view-event",
        "rank": 1,
        "surface": "current_priorities",
        "exposure_probability": 1.0,
        "visible_ratio": 0.5,
        "visible_duration_ms": 2000,
    }
    for _ in range(2):
        assert (
            client.post(
                "/api/v1/importance-impressions", headers=headers, json=body
            ).status_code
            == 204
        )
    response = client.post(
        f"/api/v1/highlights/{highlight['id']}/feedback",
        headers=headers | {"Idempotency-Key": "fatigue-is-not-negative"},
        json={"signal": "dismiss", "reason": "too_busy_to_review"},
    )
    assert response.status_code == 200, response.text
    assert len(owner_session.exec(select(ImportanceImpression)).all()) == 1
    event = owner_session.exec(
        select(ImportanceFeedbackEvent).where(
            ImportanceFeedbackEvent.highlight_id == uuid.UUID(highlight["id"]),
            ImportanceFeedbackEvent.reason == "too_busy_to_review",
        )
    ).one()
    assert event.reason == "too_busy_to_review"
    assert event.applied_delta == 0


def test_decision_explanation_exposes_review_controls(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    _, highlight = _create_highlight(
        client,
        headers,
        content="Anaphylaxis after penicillin exposure.",
    )
    response = client.get(
        f"/api/v1/highlights/{highlight['id']}/decision-explanation",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    explanation = response.json()
    assert explanation["highlight_id"] == highlight["id"]
    assert explanation["review_state"] in {
        "ready",
        "review_required",
        "abstained",
    }
    assert explanation["risk"]["effective"] == "critical"
    assert explanation["risk"]["rule_version"] == "clinical-risk-rules-v2"
    assert explanation["importance"]["protected"] is True
    assert "band" in explanation["confidence"]


def test_request_review_keeps_highlight_visible(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("staff")
    _, highlight = _create_highlight(
        client,
        auth_headers("clinician"),
        content="Follow-up blood pressure review is due.",
    )
    response = client.post(
        f"/api/v1/highlights/{highlight['id']}/request-review",
        headers=headers,
        json={"reason": "Please confirm the current follow-up date."},
    )
    assert response.status_code == 200, response.text
    assert response.json()["unresolved"] is True


def test_every_patient_publication_item_has_exact_source(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    created = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Reviewed plan",
            "content": "Continue the reviewed care plan.",
            "patient_facing": True,
        },
    )
    assert created.status_code == 201, created.text
    publication = owner_session.exec(select(PatientPublication)).one()
    items = owner_session.exec(
        select(PatientPublicationItem).where(
            PatientPublicationItem.publication_id == publication.id
        )
    ).all()
    assert items
    for item in items:
        pointer = owner_session.get(ProvenancePointer, item.provenance_pointer_id)
        assert pointer is not None
        assert pointer.anchor_state == "resolved"
        assert pointer.entry_version_id == publication.entry_version_id


@pytest.mark.unit
def test_unavailable_ai_requires_human_confirmation() -> None:
    segment = TranscriptSegmentResult(
        text="candidate fact",
        start_ms=0,
        end_ms=100,
        speaker_id=None,
        detected_language="en",
        confidence=0.75,
        confidence_source="provider",
        overlap_group_id=None,
    )
    calibrated = _apply_calibration([segment], None)[0]
    assert calibrated.confidence is None
    assert calibrated.confidence_source == "unavailable"


def test_patient_displays_approval_receipt(client: TestClient, auth_headers) -> None:
    clinician = auth_headers("clinician")
    patient = auth_headers("patient")
    patient_id = client.get("/api/v1/patients", headers=patient).json()["data"][0]["id"]
    created = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Shared plan",
            "content": "This plan was reviewed before sharing.",
            "patient_facing": True,
        },
    )
    assert created.status_code == 201, created.text
    timeline = client.get(f"/api/v1/patients/{patient_id}/timeline", headers=patient)
    assert timeline.status_code == 200, timeline.text
    receipt = next(
        item["approval_receipt"]
        for item in timeline.json()["data"]
        if item["id"] == created.json()["id"]
    )
    assert receipt["approved_by"]
    assert receipt["approved_at"]
    assert receipt["source_title"] == "Shared plan"
