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
    AuditEvent,
    ClinicalFactAssertion,
    ClinicMembership,
    DecisionAssessment,
    Entry,
    EntryVersion,
    Highlight,
    Job,
    JobAttempt,
    ProvenancePointer,
    User,
    VoiceSession,
)
from app.seed import demo_id
from app.services.ai_jobs import (
    _candidate_fingerprint,
    _create_fact_provenance,
    job_public,
)
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
