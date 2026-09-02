from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    AIRun,
    AuditEvent,
    ClinicalFactAssertion,
    ClinicMembership,
    ConflictCase,
    DecisionAssessment,
    Entry,
    EntryVersion,
    Highlight,
    Job,
    JobAttempt,
    ProvenancePointer,
    RedactionRun,
    User,
    VoiceSession,
)
from app.seed import demo_id
from app.services.ai_jobs import (
    _candidate_fingerprint,
    _create_fact_provenance,
    job_public,
)
from app.services.decisioning import ConfidenceQualification
from app.services.nightingale import rebuild_glance
from app.services.provider_resilience import ProviderFailure
from app.services.providers.base import ClinicalFact
from app.services.voice.worker import _complete_attempt


def _clinician_context(session: Session) -> RequestContext:
    user = session.get(User, demo_id("user-clinician"))
    membership = session.get(ClinicMembership, demo_id("membership-clinician"))
    assert user is not None
    assert membership is not None
    return RequestContext(user=user, membership=membership)


def _source_version(
    session: Session,
    context: RequestContext,
    *,
    patient_id: uuid.UUID,
    content: str,
) -> EntryVersion:
    entry = Entry(
        clinic_id=context.clinic_id,
        patient_id=patient_id,
        section="patient",
        origin="ai",
        entry_type="ai_patient_session_summary",
        patient_facing=False,
    )
    session.add(entry)
    session.flush()
    version_id = uuid.uuid4()
    version = EntryVersion(
        id=version_id,
        clinic_id=context.clinic_id,
        entry_id=entry.id,
        version_no=1,
        title_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "entry_version.title",
            version_id,
            "Patient statement supplied to AI",
        ),
        content_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "entry_version.content",
            version_id,
            content,
        ),
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        patient_facing=False,
        author_id=context.user_id,
    )
    session.add(version)
    session.flush()
    entry.current_version_id = version.id
    session.add(entry)
    session.flush()
    return version


def _persist_ai_patient_allergy_candidate(
    session: Session,
    context: RequestContext,
    *,
    patient_id: uuid.UUID,
    source_version: EntryVersion,
    text: str,
    needs_review: bool = False,
) -> Highlight:
    fact = ClinicalFact(
        fact_type="allergy",
        value=text,
        evidence_start=0,
        evidence_end=len(text),
        evidence_quote=text,
        feature_keys=["entity:allergy"],
    )
    highlights = _create_fact_provenance(
        session,
        context,
        job=Job(
            clinic_id=context.clinic_id,
            patient_id=patient_id,
            kind="ai_ingest",
            idempotency_key=f"candidate-{uuid.uuid4()}",
            request_sha256="a" * 64,
            payload_ciphertext=b"unused-test-payload",
            created_by_id=context.user_id,
        ),
        source_version=source_version,
        source_text=text,
        facts=[fact],
        needs_review=needs_review,
        rule_derived=False,
        provider="deterministic",
        model="clinical-fixture-v1",
    )
    assert len(highlights) == 1
    return highlights[0]


@pytest.mark.parametrize("ai_first", [False, True])
def test_nurse_allergy_and_patient_via_ai_nkda_are_critical_without_mutating_anchor(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    ai_first: bool,
) -> None:
    clinician_headers = auth_headers("clinician")
    staff_headers = auth_headers("staff")
    patient_id = uuid.UUID(
        client.get("/api/v1/patients", headers=clinician_headers).json()["data"][0][
            "id"
        ]
    )
    context = _clinician_context(owner_session)
    ai_text = "No known drug allergies"
    source_version = _source_version(
        owner_session,
        context,
        patient_id=patient_id,
        content=ai_text,
    )
    owner_session.commit()

    def add_ai_statement() -> Highlight:
        highlight = _persist_ai_patient_allergy_candidate(
            owner_session,
            context,
            patient_id=patient_id,
            source_version=source_version,
            text=ai_text,
        )
        owner_session.commit()
        return highlight

    def add_nurse_statement() -> None:
        response = client.post(
            "/api/v1/entries",
            headers=staff_headers,
            json={
                "patient_id": str(patient_id),
                "section": "staff",
                "title": "Nurse allergy history",
                "content": "Patient is allergic to penicillin.",
                "patient_facing": False,
            },
        )
        assert response.status_code == 201, response.text

    if ai_first:
        ai_highlight = add_ai_statement()
        add_nurse_statement()
    else:
        add_nurse_statement()
        owner_session.expire_all()
        ai_highlight = add_ai_statement()

    owner_session.expire_all()
    stored_highlight = owner_session.get(Highlight, ai_highlight.id)
    assert stored_highlight is not None
    assert stored_highlight.critical is False
    assert stored_highlight.unresolved is True
    assert stored_highlight.candidate_fingerprint is not None

    conflict_response = client.get(
        f"/api/v1/patients/{patient_id}/conflicts", headers=clinician_headers
    )
    assert conflict_response.status_code == 200, conflict_response.text
    unresolved = [
        item for item in conflict_response.json() if item["status"] == "unresolved"
    ]
    assert len(unresolved) == 1
    conflict = unresolved[0]
    assert conflict["severity"] == "critical"
    assert {conflict["left_origin"], conflict["right_origin"]} == {"human", "ai"}
    assert {conflict["left_source_role"], conflict["right_source_role"]} == {
        "staff",
        "patient",
    }
    assert {conflict["left_assertion_scope"], conflict["right_assertion_scope"]} == {
        "drug_allergies",
        "specific_substance",
    }
    assert {
        conflict["left_allergy_category"],
        conflict["right_allergy_category"],
    } == {"drug"}

    rebuild_glance(owner_session, context, patient_id)
    owner_session.commit()
    glance = client.get(
        f"/api/v1/patients/{patient_id}/glance", headers=clinician_headers
    )
    assert glance.status_code == 200, glance.text
    projected = next(
        card
        for card in glance.json()["review_cards"]
        if card["highlight_id"] == str(ai_highlight.id)
    )
    assert projected["critical"] is True
    assert projected["importance"]["protected"] is True
    assert projected["risk"]["effective"] == "critical"
    assert "ALLERGY_CONFLICT" in projected["risk"]["rule_ids"]


def test_ai_candidate_fingerprint_reuses_clinician_state_without_duplicates(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_id = uuid.UUID(
        client.get("/api/v1/patients", headers=clinician_headers).json()["data"][0][
            "id"
        ]
    )
    context = _clinician_context(owner_session)
    text = "Patient is allergic to penicillin."
    source_version = _source_version(
        owner_session,
        context,
        patient_id=patient_id,
        content=text,
    )
    first = _persist_ai_patient_allergy_candidate(
        owner_session,
        context,
        patient_id=patient_id,
        source_version=source_version,
        text=text,
    )
    owner_session.commit()

    first.status = "accepted"
    first.pinned = True
    first.clinician_confirmed = True
    owner_session.add(first)
    owner_session.commit()

    second = _persist_ai_patient_allergy_candidate(
        owner_session,
        context,
        patient_id=patient_id,
        source_version=source_version,
        text=text,
        needs_review=True,
    )
    owner_session.commit()
    owner_session.expire_all()

    assert second.id == first.id
    stored = owner_session.get(Highlight, first.id)
    assert stored is not None
    fingerprint = stored.candidate_fingerprint
    assert fingerprint is not None
    assert len(fingerprint) == 64
    candidates = owner_session.exec(
        select(Highlight).where(
            Highlight.clinic_id == context.clinic_id,
            Highlight.candidate_fingerprint == fingerprint,
        )
    ).all()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.status == "accepted"
    assert candidate.pinned is True
    assert candidate.clinician_confirmed is True
    assert candidate.unresolved is True
    assert (
        len(
            owner_session.exec(
                select(ProvenancePointer).where(
                    ProvenancePointer.highlight_id == candidate.id
                )
            ).all()
        )
        == 1
    )
    assert (
        len(
            owner_session.exec(
                select(ClinicalFactAssertion).where(
                    ClinicalFactAssertion.highlight_id == candidate.id
                )
            ).all()
        )
        == 1
    )
    assert (
        len(
            owner_session.exec(
                select(DecisionAssessment).where(
                    DecisionAssessment.highlight_id == candidate.id
                )
            ).all()
        )
        == 1
    )


@pytest.mark.unit
def test_ai_candidate_fingerprint_is_semantic_and_stable_for_shared_span() -> None:
    source_version_id = uuid.uuid4()
    quote = "Patient is allergic to penicillin and aspirin."

    def fingerprint(value: str) -> str:
        return _candidate_fingerprint(
            source_version_id,
            ClinicalFact(
                fact_type="allergy",
                value=value,
                evidence_start=0,
                evidence_end=len(quote),
                evidence_quote=quote,
            ),
            quote,
        )

    # Provider casing/spacing changes do not mint a new candidate identity.
    assert fingerprint("Penicillin") == fingerprint("  penicillin  ")
    # Two normalized entities on one immutable evidence span remain distinct.
    assert fingerprint("penicillin") != fingerprint("aspirin")


def test_retryable_audio_failure_keeps_processing_and_projects_audio_circuit(
    owner_session: Session,
) -> None:
    context = _clinician_context(owner_session)
    patient_id = demo_id("patient-primary")
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        clinic_id=context.clinic_id,
        patient_id=patient_id,
        kind="voice_process",
        state="running",
        idempotency_key=f"voice-retry-{job_id}",
        request_sha256="b" * 64,
        payload_ciphertext=field_codec.encrypt_json(
            context.clinic_id, "job.payload", job_id, {"fixture": True}
        ),
        attempt_count=1,
        max_attempts=5,
        created_by_id=context.user_id,
    )
    voice_session = VoiceSession(
        clinic_id=context.clinic_id,
        patient_id=patient_id,
        capture_kind="clinical",
        state="transcribing",
        synthetic_fixture=True,
        fixture_id="retry-projection-v1",
        created_by_id=context.user_id,
        processing_job_id=job.id,
        error_code="PROVIDER_TIMEOUT",
    )
    attempt = JobAttempt(
        clinic_id=context.clinic_id,
        job_id=job.id,
        worker_membership_id=context.membership.id,
        attempt_no=1,
    )
    owner_session.add(job)
    owner_session.add(voice_session)
    owner_session.add(attempt)
    owner_session.flush()

    completed = _complete_attempt(
        owner_session,
        context,
        job,
        attempt,
        voice_session,
        needs_review=True,
        provider_failure=ProviderFailure(
            code="PROVIDER_TIMEOUT",
            failure_class="timeout",
            retryable=True,
        ),
    )
    owner_session.expire_all()

    stored_session = owner_session.get(VoiceSession, voice_session.id)
    assert stored_session is not None
    assert stored_session.state == "transcribing"
    assert completed.state == "failed"
    assert completed.next_run_at is not None
    public = job_public(owner_session, completed)
    assert public.visible_state == "delayed"
    assert public.provider_outage is True
    assert public.retry_after_seconds is not None
    assert public.retry_history[-1].capability == "audio_transcription"
    assert public.retry_history[-1].provider == "openai"
    assert (
        owner_session.exec(
            select(AuditEvent).where(
                AuditEvent.resource_id == stored_session.id,
                AuditEvent.action == "voice.processing_delayed",
            )
        ).first()
        is not None
    )


def test_candidate_fingerprint_is_database_immutable(owner_session: Session) -> None:
    context = _clinician_context(owner_session)
    patient_id = demo_id("patient-primary")
    text = "Patient is allergic to penicillin."
    source_version = _source_version(
        owner_session,
        context,
        patient_id=patient_id,
        content=text,
    )
    candidate = _persist_ai_patient_allergy_candidate(
        owner_session,
        context,
        patient_id=patient_id,
        source_version=source_version,
        text=text,
    )
    owner_session.commit()

    with pytest.raises(DBAPIError, match="immutable highlight candidate fingerprint"):
        candidate.candidate_fingerprint = "f" * 64
        owner_session.add(candidate)
        owner_session.commit()
    owner_session.rollback()


def test_model_derived_highlight_without_assessment_never_reaches_priorities(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    """A missing assessment is an unqualified claim, not an absent one.

    The AI job always writes an assessment beside the candidate, so this state
    is reachable only through partial or legacy data. It must still fail closed
    rather than presenting an uncalibrated model claim as a ready priority.
    """

    clinician_headers = auth_headers("clinician")
    patient_id = uuid.UUID(
        client.get("/api/v1/patients", headers=clinician_headers).json()["data"][0][
            "id"
        ]
    )
    context = _clinician_context(owner_session)
    text = "Patient reports an allergy to penicillin."
    source_version = _source_version(
        owner_session,
        context,
        patient_id=patient_id,
        content=text,
    )
    highlight = _persist_ai_patient_allergy_candidate(
        owner_session,
        context,
        patient_id=patient_id,
        source_version=source_version,
        text=text,
    )
    # Promote the candidate, then strip its assessment while leaving the
    # model-derived marker in place.
    highlight.status = "accepted"
    highlight.unresolved = False
    highlight.review_required = False
    highlight.support_review_required = False
    highlight.current_priority_eligible = True
    owner_session.add(highlight)
    assessment = owner_session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.clinic_id == context.clinic_id,
            DecisionAssessment.highlight_id == highlight.id,
        )
    ).one()
    owner_session.delete(assessment)
    owner_session.commit()

    owner_session.expire_all()
    rebuild_glance(owner_session, context, patient_id)
    owner_session.commit()
    glance = client.get(
        f"/api/v1/patients/{patient_id}/glance", headers=clinician_headers
    )
    assert glance.status_code == 200, glance.text
    body = glance.json()
    assert str(highlight.id) not in {card["highlight_id"] for card in body["cards"]}
    projected = next(
        card
        for card in body["review_cards"]
        if card["highlight_id"] == str(highlight.id)
    )
    assert projected["current_confidence_state"] == "review_required"
    assert "AI_HIGHLIGHT_ASSESSMENT_MISSING" in projected["current_confidence_reasons"]
    assert body["safety_review_required"] is True


def test_human_priorities_without_assessment_remain_displayable(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
) -> None:
    """Human-authored priorities have no calibration claim and keep working."""

    clinician_headers = auth_headers("clinician")
    staff_headers = auth_headers("staff")
    created_patient = client.post(
        "/api/v1/patients",
        headers=staff_headers,
        json={
            "display_name": "Human Priority Probe",
            "date_of_birth": "1988-04-12",
            "medical_record_number": f"MRN-HP-{uuid.uuid4().hex[:8]}",
            "identity_document_type": "nric_fin",
            "identity_document_number": f"S{uuid.uuid4().int % 10_000_000:07d}A",
        },
    )
    assert created_patient.status_code == 201, created_patient.text
    patient_id = created_patient.json()["id"]
    created = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Follow-up reminder",
            "content": "Continue current plan.",
            "patient_facing": False,
        },
    )
    assert created.status_code == 201, created.text
    entry = created.json()
    highlighted = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=clinician_headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": len("Continue current plan."),
            "exact_quote": "Continue current plan.",
            "label": "Current plan",
            "feature_keys": ["topic:follow_up"],
            "patient_facing": False,
        },
    )
    assert highlighted.status_code == 201, highlighted.text
    highlight_id = highlighted.json()["id"]
    accepted = client.post(
        f"/api/v1/highlights/{highlight_id}/accept",
        headers=staff_headers,
    )
    assert accepted.status_code == 200, accepted.text

    glance = client.get(
        f"/api/v1/patients/{patient_id}/glance", headers=clinician_headers
    )
    assert glance.status_code == 200, glance.text
    body = glance.json()
    projected = next(
        card for card in body["cards"] if card["highlight_id"] == highlight_id
    )
    assert projected["current_confidence_state"] == "unavailable"
    assert "CONFIDENCE_NOT_APPLICABLE" in projected["current_confidence_reasons"]
    assert highlight_id not in {card["highlight_id"] for card in body["review_cards"]}


def _persist_two_ai_facts(
    session: Session,
    context: RequestContext,
    *,
    patient_id: uuid.UUID,
) -> tuple[Job, EntryVersion, list[Highlight]]:
    first = "Patient is allergic to clindamycin."
    second = "Patient is allergic to latex."
    text = f"{first} {second}"
    source_version = _source_version(
        session,
        context,
        patient_id=patient_id,
        content=text,
    )
    job = Job(
        clinic_id=context.clinic_id,
        patient_id=patient_id,
        kind="ai_ingest",
        idempotency_key=f"coverage-{uuid.uuid4()}",
        request_sha256="a" * 64,
        payload_ciphertext=b"unused-test-payload",
        created_by_id=context.user_id,
        state="succeeded",
    )
    session.add(job)
    session.flush()
    redaction = RedactionRun(
        clinic_id=context.clinic_id,
        source_entry_version_id=source_version.id,
        status="completed",
        input_sha256="a" * 64,
        redacted_sha256="b" * 64,
        map_ciphertext=b"{}",
        residual_scan_passed=True,
    )
    session.add(redaction)
    session.flush()
    session.add(
        AIRun(
            clinic_id=context.clinic_id,
            patient_id=patient_id,
            job_id=job.id,
            redaction_run_id=redaction.id,
            source_entry_version_id=source_version.id,
            interaction_type="care_note",
            provider="deterministic",
            model="clinical-fixture-v1",
            status="completed",
            request_sha256="a" * 64,
        )
    )
    highlights = _create_fact_provenance(
        session,
        context,
        job=job,
        source_version=source_version,
        source_text=text,
        facts=[
            ClinicalFact(
                fact_type="allergy",
                value="clindamycin",
                evidence_start=0,
                evidence_end=len(first),
                evidence_quote=first,
                feature_keys=["entity:allergy"],
            ),
            ClinicalFact(
                fact_type="allergy",
                value="latex",
                evidence_start=len(first) + 1,
                evidence_end=len(text),
                evidence_quote=second,
                feature_keys=["entity:allergy"],
            ),
        ],
        needs_review=False,
        rule_derived=False,
        provider="deterministic",
        model="clinical-fixture-v1",
    )
    assert len(highlights) == 2
    return job, source_version, highlights


def test_incomplete_ai_assessment_coverage_keeps_job_review_required(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_id = uuid.UUID(
        client.get("/api/v1/patients", headers=clinician_headers).json()["data"][0][
            "id"
        ]
    )
    context = _clinician_context(owner_session)
    job, _source_version, highlights = _persist_two_ai_facts(
        owner_session,
        context,
        patient_id=patient_id,
    )
    dropped = owner_session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.clinic_id == context.clinic_id,
            DecisionAssessment.highlight_id == highlights[1].id,
        )
    ).one()
    owner_session.delete(dropped)
    owner_session.commit()

    owner_session.expire_all()
    stored_job = owner_session.get(Job, job.id)
    assert stored_job is not None
    projected = job_public(owner_session, stored_job)
    assert projected.current_confidence_state == "review_required"
    assert (
        "JOB_CONFIDENCE_ASSESSMENT_INCOMPLETE" in projected.current_confidence_reasons
    )
    assert projected.safety_review_required is True


def test_partially_assessed_ai_entry_cannot_publish(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_id = uuid.UUID(
        client.get("/api/v1/patients", headers=clinician_headers).json()["data"][0][
            "id"
        ]
    )
    context = _clinician_context(owner_session)
    _job, source_version, highlights = _persist_two_ai_facts(
        owner_session,
        context,
        patient_id=patient_id,
    )
    dropped = owner_session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.clinic_id == context.clinic_id,
            DecisionAssessment.highlight_id == highlights[1].id,
        )
    ).one()
    owner_session.delete(dropped)
    kept = owner_session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.clinic_id == context.clinic_id,
            DecisionAssessment.highlight_id == highlights[0].id,
        )
    ).one()
    kept.abstained = False
    kept.support_state = "supported"
    kept.confidence_band = "medium"
    owner_session.add(kept)
    for conflict in owner_session.exec(
        select(ConflictCase).where(
            ConflictCase.clinic_id == context.clinic_id,
            ConflictCase.patient_id == patient_id,
            ConflictCase.status == "unresolved",
        )
    ).all():
        conflict.status = "resolved"
        owner_session.add(conflict)
    owner_session.commit()

    monkeypatch.setattr(
        "app.api.routes.trust.redaction_is_qualified",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "app.api.routes.trust.requalify_assessment_confidence",
        lambda *_args, **_kwargs: ConfidenceQualification(
            qualified=True,
            current_state="qualified",
            band="medium",
            lower_bound=0.9,
            reasons=(),
        ),
    )

    published = client.post(
        f"/api/v1/entries/{source_version.entry_id}/patient-publications",
        headers=clinician_headers,
        json={"entry_version_id": str(source_version.id)},
    )
    assert published.status_code == 409, published.text
    assert published.json()["detail"]["code"] == "CLAIM_LEVEL_PROVENANCE_REQUIRED"


def test_patient_authored_note_can_create_its_own_provenance(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    """A patient's own words must reach the record, evidence and all.

    Fact extraction runs on every entry, so a patient insight that mentions an
    allergy also writes a ProvenancePointer. The pointer's row-level check used
    to test the pointer's own linkage, which cannot be satisfied before the row
    exists, so the whole request failed with a 500 and the patient could not
    file anything clinically meaningful.
    """

    patient_headers = auth_headers("patient")
    patient_id = client.get("/api/v1/patients", headers=patient_headers).json()["data"][
        0
    ]["id"]

    response = client.post(
        "/api/v1/entries",
        headers=patient_headers,
        json={
            "patient_id": patient_id,
            "section": "patient",
            "title": "About my allergies",
            "content": "I have no known drug allergies.",
            "patient_facing": True,
        },
    )
    assert response.status_code == 201, response.text

    owner_session.expire_all()
    pointer_count = len(
        owner_session.exec(
            select(ProvenancePointer).where(
                ProvenancePointer.entry_version_id
                == uuid.UUID(response.json()["version_id"])
            )
        ).all()
    )
    assert pointer_count >= 1


def test_patient_may_contradict_the_record_and_the_conflict_is_kept(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
) -> None:
    """The patient's own account is inbound, so a disagreement cannot silence it.

    The unresolved-conflict gate stops clinical content being pushed out to a
    patient. It used to fire on the patient's own insight as well, so a patient
    whose statement contradicted the chart was refused and the whole entry rolled
    back, leaving the record holding only one side. The contradiction is exactly
    what the clinician needs to see, so it is recorded and surfaced instead.
    """

    staff_headers = auth_headers("staff")
    patient_headers = auth_headers("patient")
    clinician_headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=patient_headers).json()["data"][
        0
    ]["id"]

    nurse_note = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Allergy history",
            "content": "Patient reports penicillin allergy.",
            "patient_facing": False,
        },
    )
    assert nurse_note.status_code == 201, nurse_note.text

    patient_insight = client.post(
        "/api/v1/entries",
        headers=patient_headers,
        json={
            "patient_id": patient_id,
            "section": "patient",
            "title": "About my allergies",
            "content": "I have no known drug allergies.",
            "patient_facing": True,
        },
    )
    assert patient_insight.status_code == 201, patient_insight.text

    conflicts = client.get(
        f"/api/v1/patients/{patient_id}/conflicts", headers=clinician_headers
    )
    assert conflicts.status_code == 200, conflicts.text
    unresolved = [
        item
        for item in conflicts.json()
        if item["status"] == "unresolved" and item["fact_type"] == "allergy"
    ]
    assert len(unresolved) == 1
    case = unresolved[0]
    assert case["severity"] == "critical"
    # Neither side wins, and each names who asserted it.
    assert {case["left_source_role"], case["right_source_role"]} == {"staff", "patient"}

    # A clinician-authored note still cannot be shared while that conflict stands.
    blocked = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Pre-procedure summary",
            "content": "Proceed as planned.",
            "patient_facing": True,
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "UNRESOLVED_CLINICAL_CONFLICT"
