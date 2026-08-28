from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    AIRun,
    AuditEvent,
    CareTask,
    Clinic,
    ClinicalFactAssertion,
    ClinicMembership,
    Comment,
    CommentMention,
    ConflictCase,
    DecisionAssessment,
    Entry,
    EntryVersion,
    Highlight,
    ImportanceFeatureStat,
    ImportanceFeedbackEvent,
    Patient,
    PatientGlanceSnapshot,
    User,
)
from app.seed import demo_id, seed_demo_data
from app.services.decay import list_decay_candidates
from app.services.nightingale import read_glance, rebuild_glance


def test_full_demo_fixture_is_idempotent_and_covers_delivery_scenarios(
    owner_session: Session,
) -> None:
    seed_demo_data(owner_session)
    seed_demo_data(owner_session)

    entries = owner_session.exec(
        select(Entry).where(Entry.clinic_id == demo_id("clinic-primary"))
    ).all()
    entry_types = {entry.entry_type for entry in entries}
    assert {
        "manual_staff_note",
        "manual_clinician_note",
        "ai_doctor_consult_summary",
        "ai_nurse_consult_summary",
        "ai_patient_session_summary",
    } <= entry_types
    ai_runs = owner_session.exec(
        select(AIRun).where(AIRun.clinic_id == demo_id("clinic-primary"))
    ).all()
    interaction_by_output = {
        run.output_entry_id: run.interaction_type for run in ai_runs
    }
    for entry in entries:
        if entry.entry_type == "ai_doctor_consult_summary":
            assert interaction_by_output[entry.id] == "doctor_consult"
        elif entry.entry_type == "ai_nurse_consult_summary":
            assert interaction_by_output[entry.id] == "care_note"
        elif entry.entry_type == "ai_patient_session_summary":
            assert interaction_by_output[entry.id] in {
                "patient_insight",
                "voice_session",
            }
    assert {entry.occurred_at.date().isoformat() for entry in entries} >= {
        "2023-01-10",
        "2025-04-15",
        "2026-02-06",
    }

    snapshot = owner_session.get(PatientGlanceSnapshot, demo_id("glance-primary"))
    assert snapshot is not None
    cards, _ = read_glance(snapshot)
    assert 3 <= len(cards) <= 5
    assert all(card["provenance_pointer_id"] for card in cards)
    assert any(card["critical"] for card in cards)
    ai_review = owner_session.get(Highlight, demo_id("highlight-ai-doctor-review"))
    assert ai_review is not None
    ai_source = owner_session.get(Entry, ai_review.entry_id)
    assert ai_source is not None
    assert ai_source.origin == "ai"
    assert ai_source.entry_type == "ai_doctor_consult_summary"
    assert str(ai_review.id) in {card["highlight_id"] for card in cards}

    assert owner_session.get(Comment, demo_id("comment-clinician-assignment"))
    assert owner_session.get(CommentMention, demo_id("comment-mention-clinician"))
    assert owner_session.get(CareTask, demo_id("task-fall-risk-review"))
    assert owner_session.get(AuditEvent, demo_id("audit-demo-fixture"))
    assert owner_session.get(
        ImportanceFeedbackEvent, demo_id("importance-feedback-medication")
    )

    clinic = owner_session.get(Clinic, demo_id("clinic-primary"))
    primary_patient = owner_session.get(Patient, demo_id("patient-primary"))
    other_patient = owner_session.get(Patient, demo_id("patient-other"))
    decay_patient = owner_session.get(Patient, demo_id("patient-decay"))
    assert clinic is not None
    assert primary_patient is not None
    assert other_patient is not None
    assert decay_patient is not None
    assert clinic.name == "Nightingale Clinic"
    assert (
        field_codec.decrypt_text(
            primary_patient.clinic_id,
            "patient.display_name",
            primary_patient.id,
            primary_patient.display_name_ciphertext,
        )
        == "Alex Tan"
    )
    assert (
        field_codec.decrypt_text(
            other_patient.clinic_id,
            "patient.display_name",
            other_patient.id,
            other_patient.display_name_ciphertext,
        )
        == "Taylor Lee"
    )
    assert (
        field_codec.decrypt_text(
            decay_patient.clinic_id,
            "patient.display_name",
            decay_patient.id,
            decay_patient.display_name_ciphertext,
        )
        == "Jordan Wong"
    )

    visible_copy: list[str] = []
    for version in owner_session.exec(select(EntryVersion)).all():
        visible_copy.extend(
            [
                field_codec.decrypt_text(
                    version.clinic_id,
                    "entry_version.title",
                    version.id,
                    version.title_ciphertext,
                ),
                field_codec.decrypt_text(
                    version.clinic_id,
                    "entry_version.content",
                    version.id,
                    version.content_ciphertext,
                ),
            ]
        )
    comment = owner_session.get(Comment, demo_id("comment-clinician-assignment"))
    task = owner_session.get(CareTask, demo_id("task-fall-risk-review"))
    assert comment is not None and task is not None
    visible_copy.extend(
        [
            field_codec.decrypt_text(
                comment.clinic_id,
                "comment.body",
                comment.id,
                comment.body_ciphertext,
            ),
            field_codec.decrypt_text(
                task.clinic_id,
                "care_task.title",
                task.id,
                task.title_ciphertext,
            ),
        ]
    )
    assert all(
        term not in value.lower()
        for value in visible_copy
        for term in ("synthetic", "fixture", "demo")
    )


def test_retention_history_is_archivable_without_weakening_active_care_protection(
    owner_session: Session,
) -> None:
    seed_demo_data(owner_session)
    clinician = owner_session.get(User, demo_id("user-clinician"))
    membership = owner_session.get(ClinicMembership, demo_id("membership-clinician"))
    assert clinician is not None and membership is not None

    candidates = list_decay_candidates(
        owner_session,
        RequestContext(user=clinician, membership=membership),
    )
    by_version = {item.entry_version_id: item for item in candidates}

    retention_version_id = demo_id("entry-retention-history-2023-version-1")
    retention = by_version[retention_version_id]
    assert retention.protected_reasons == []
    assert retention.eligible_for_cold is True

    active_care_version_id = demo_id("entry-decay-candidate-2023-version-1")
    active_care = by_version[active_care_version_id]
    assert "open_task" in active_care.protected_reasons
    assert active_care.eligible_for_cold is False


def test_longitudinal_patient_fixture_is_visible_source_linked_and_collaborative(
    owner_session: Session,
) -> None:
    seed_demo_data(owner_session)
    patient_id = demo_id("patient-decay")

    entries = owner_session.exec(
        select(Entry).where(
            Entry.clinic_id == demo_id("clinic-primary"),
            Entry.patient_id == patient_id,
        )
    ).all()
    assert len(entries) >= 10
    assert min(entry.occurred_at.year for entry in entries) == 2004
    assert max(entry.occurred_at.year for entry in entries) == 2026
    assert {
        "ai_doctor_consult_summary",
        "ai_nurse_consult_summary",
        "ai_patient_session_summary",
    } <= {entry.entry_type for entry in entries}

    versioned_entry = owner_session.get(Entry, demo_id("entry-jordan-acute-plan-2026"))
    assert versioned_entry is not None
    versions = owner_session.exec(
        select(EntryVersion).where(EntryVersion.entry_id == versioned_entry.id)
    ).all()
    assert [version.version_no for version in versions] == [1, 2, 3]

    conflict = owner_session.get(
        ConflictCase, demo_id("conflict-jordan-oral-intake-plan")
    )
    assert conflict is not None
    assert conflict.status == "unresolved"
    assert conflict.severity == "high"
    assert conflict.left_pointer_id is not None
    assert conflict.right_pointer_id is not None

    facts = owner_session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.patient_id == patient_id
        )
    ).all()
    assert len(facts) >= 7
    assert {fact.origin for fact in facts} >= {"human", "ai"}
    assert all(fact.provenance_pointer_id for fact in facts)

    conflict_highlight = owner_session.get(
        Highlight, demo_id("highlight-jordan-oral-intake-conflict")
    )
    assert conflict_highlight is not None
    conflict_assessment = owner_session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.highlight_id == conflict_highlight.id
        )
    ).one()
    assert conflict_assessment.abstained is True
    assert conflict_assessment.abstention_reason == "UNRESOLVED_HIGH_RISK_CONFLICT"

    snapshot = owner_session.get(PatientGlanceSnapshot, demo_id("glance-decay"))
    assert snapshot is not None
    cards, _ = read_glance(snapshot)
    assert len(cards) == 5
    assert any("AI-scribed handover" in str(card["label"]) for card in cards)
    assert all(card["provenance_pointer_id"] for card in cards)

    assert owner_session.get(Comment, demo_id("comment-jordan-hydration-conflict"))
    assert owner_session.get(
        CommentMention, demo_id("comment-mention-jordan-clinician")
    )
    assert owner_session.get(CareTask, demo_id("task-jordan-hydration-review"))


def test_other_clinic_fixture_has_independent_realistic_patient_records(
    owner_session: Session,
) -> None:
    seed_demo_data(owner_session)

    clinic = owner_session.get(Clinic, demo_id("clinic-other"))
    assert clinic is not None
    assert clinic.code == "OTHERCLINIC"
    assert clinic.name == "Harbour Family Clinic"

    patients = owner_session.exec(
        select(Patient).where(Patient.clinic_id == clinic.id)
    ).all()
    assert len(patients) >= 303
    names = {
        field_codec.decrypt_text(
            patient.clinic_id,
            "patient.display_name",
            patient.id,
            patient.display_name_ciphertext,
        )
        for patient in patients
    }
    assert {"Taylor Lee", "Priya Nair", "Daniel Koh"} <= names

    other_entries = owner_session.exec(
        select(Entry).where(Entry.clinic_id == clinic.id)
    ).all()
    assert len(other_entries) == 6
    assert {entry.patient_id for entry in other_entries} == {
        demo_id("patient-other"),
        demo_id("patient-other-priya"),
        demo_id("patient-other-daniel"),
    }

    conflict = owner_session.get(
        ConflictCase, demo_id("conflict-other-priya-medication-status")
    )
    assert conflict is not None
    assert conflict.clinic_id == clinic.id
    assert conflict.status == "unresolved"
    assert conflict.severity == "high"
    assert conflict.left_pointer_id is not None
    assert conflict.right_pointer_id is not None

    for patient_id in (
        demo_id("patient-other"),
        demo_id("patient-other-priya"),
        demo_id("patient-other-daniel"),
    ):
        snapshot = owner_session.exec(
            select(PatientGlanceSnapshot).where(
                PatientGlanceSnapshot.clinic_id == clinic.id,
                PatientGlanceSnapshot.patient_id == patient_id,
            )
        ).one()
        cards, _ = read_glance(snapshot)
        assert cards
        assert all(card["provenance_pointer_id"] for card in cards)


def test_other_clinic_examples_are_visible_only_through_other_clinic_login(
    owner_session: Session, client
) -> None:
    seed_demo_data(owner_session)

    other_login = client.post(
        "/api/v1/auth/login",
        headers={"X-Clinic-Code": "OTHERCLINIC"},
        data={
            "username": "clinician@other-clinic.example",
            "password": "synthetic-demo-only",
        },
    )
    assert other_login.status_code == 200, other_login.text
    other_token = other_login.json()["access_token"]
    other_patients = client.get(
        "/api/v1/patients/?search=Taylor%20Lee",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert other_patients.status_code == 200, other_patients.text
    assert other_patients.json()["count"] == 1
    assert other_patients.json()["data"][0]["display_name"] == "Taylor Lee"
    assert other_patients.json()["data"][0]["medical_record_number"] == "MRN2026002"

    primary_login = client.post(
        "/api/v1/auth/login",
        headers={"X-Clinic-Code": "NIGHTINGALE"},
        data={
            "username": "clinician@nightingale.example",
            "password": "synthetic-demo-only",
        },
    )
    assert primary_login.status_code == 200, primary_login.text
    primary_token = primary_login.json()["access_token"]
    primary_patients = client.get(
        "/api/v1/patients/?search=Taylor%20Lee",
        headers={"Authorization": f"Bearer {primary_token}"},
    )
    assert primary_patients.status_code == 200, primary_patients.text
    assert primary_patients.json()["count"] == 0


def test_patient_directory_search_pagination_and_same_name_warning(
    owner_session: Session, client
) -> None:
    seed_demo_data(owner_session)
    login = client.post(
        "/api/v1/auth/login",
        headers={"X-Clinic-Code": "OTHERCLINIC"},
        data={
            "username": "clinician@other-clinic.example",
            "password": "synthetic-demo-only",
        },
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    page = client.get("/api/v1/patients/?offset=0&limit=24", headers=headers)
    assert page.status_code == 200, page.text
    assert page.json()["count"] >= 303
    assert len(page.json()["data"]) == 24
    assert page.json()["offset"] == 0
    today = client.get("/api/v1/patients/?visit_scope=today&limit=100", headers=headers)
    assert today.status_code == 200, today.text
    assert today.json()["count"] == 6
    assert all(item["today_visit_at"] for item in today.json()["data"])
    assert [item["today_visit_at"] for item in today.json()["data"]] == sorted(
        item["today_visit_at"] for item in today.json()["data"]
    )
    previous = client.get(
        "/api/v1/patients/?visit_scope=previous&limit=24", headers=headers
    )
    assert previous.status_code == 200, previous.text
    assert previous.json()["count"] == page.json()["count"] - today.json()["count"]
    assert all(item["today_visit_at"] is None for item in previous.json()["data"])
    duplicate_name = client.get(
        "/api/v1/patients/?search=Jamie%20Tan&limit=100", headers=headers
    )
    assert duplicate_name.status_code == 200
    assert duplicate_name.json()["count"] == 2
    assert all(item["same_name_count"] == 2 for item in duplicate_name.json()["data"])
    assert (
        len({item["medical_record_number"] for item in duplicate_name.json()["data"]})
        == duplicate_name.json()["count"]
    )


def test_source_linked_clinical_context_is_visible_only_to_care_team(
    owner_session: Session, client, auth_headers
) -> None:
    seed_demo_data(owner_session)
    patient_id = demo_id("patient-decay")

    staff_response = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts",
        headers=auth_headers("staff"),
    )
    assert staff_response.status_code == 200
    facts = staff_response.json()
    assert len(facts) >= 7
    assert all(fact["provenance_pointer_id"] for fact in facts)
    assert {fact["origin"] for fact in facts} >= {"human", "ai"}
    assert {
        "obesity",
        "type 2 diabetes",
        "acute pancreatitis",
        "oral intake",
    } <= {fact["subject"] for fact in facts}

    patient_response = client.get(
        f"/api/v1/patients/{patient_id}/clinical-facts",
        headers=auth_headers("patient"),
    )
    assert patient_response.status_code == 403


def test_reseed_preserves_feedback_and_rebuilds_snapshot_from_current_state(
    owner_session: Session,
) -> None:
    def stable_card_projection(
        cards: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Exclude the clock-derived recency values from a reseed comparison.

        Rebuilding Glance deliberately recalculates age-based importance against
        the current clock.  A reseed must preserve card identity, ordering,
        decisions, sources, and learned state; it is not expected to freeze a
        previously computed recency decimal.
        """

        output: list[dict[str, object]] = []
        for card in cards:
            normalized = dict(card)
            score_components_value = normalized.get("score_components", {})
            assert isinstance(score_components_value, dict)
            score_components = dict(score_components_value)
            score_components.pop("recency", None)
            score_components.pop("final", None)
            normalized["score_components"] = score_components

            importance_value = normalized.get("importance", {})
            assert isinstance(importance_value, dict)
            importance = dict(importance_value)
            importance.pop("score", None)
            importance_components_value = importance.get("components", {})
            assert isinstance(importance_components_value, dict)
            importance_components = dict(importance_components_value)
            importance_components.pop("recency", None)
            importance_components.pop("final", None)
            importance["components"] = importance_components
            normalized["importance"] = importance
            output.append(normalized)
        return output

    seed_demo_data(owner_session)
    ai_review = owner_session.get(Highlight, demo_id("highlight-ai-doctor-review"))
    feedback = owner_session.get(
        ImportanceFeedbackEvent, demo_id("importance-feedback-medication")
    )
    stat = owner_session.get(
        ImportanceFeatureStat, demo_id("importance-stat-medication")
    )
    clinician = owner_session.get(User, demo_id("user-clinician"))
    membership = owner_session.get(ClinicMembership, demo_id("membership-clinician"))
    assert ai_review is not None and feedback is not None and stat is not None
    assert clinician is not None and membership is not None

    ai_review.status = "rejected"
    ai_review.pinned = False
    feedback.signal = "reject"
    feedback.applied_delta = -0.03
    stat.weight = -0.17
    owner_session.add_all([ai_review, feedback, stat])
    rebuild_glance(
        owner_session,
        RequestContext(user=clinician, membership=membership),
        demo_id("patient-primary"),
    )
    owner_session.commit()

    snapshot = owner_session.get(PatientGlanceSnapshot, demo_id("glance-primary"))
    assert snapshot is not None
    cards_before, _ = read_glance(snapshot)
    assert str(ai_review.id) not in {card["highlight_id"] for card in cards_before}
    counts_before = {
        "entries": len(owner_session.exec(select(Entry)).all()),
        "feedback": len(owner_session.exec(select(ImportanceFeedbackEvent)).all()),
        "highlights": len(owner_session.exec(select(Highlight)).all()),
    }

    seed_demo_data(owner_session)
    seed_demo_data(owner_session)
    owner_session.expire_all()

    preserved_ai_review = owner_session.get(Highlight, ai_review.id)
    preserved_feedback = owner_session.get(ImportanceFeedbackEvent, feedback.id)
    preserved_stat = owner_session.get(ImportanceFeatureStat, stat.id)
    snapshot = owner_session.get(PatientGlanceSnapshot, demo_id("glance-primary"))
    assert preserved_ai_review is not None and preserved_feedback is not None
    assert preserved_stat is not None and snapshot is not None
    cards_after, _ = read_glance(snapshot)
    assert preserved_ai_review.status == "rejected"
    assert preserved_ai_review.pinned is False
    assert preserved_feedback.signal == "reject"
    assert preserved_feedback.applied_delta == -0.03
    assert preserved_stat.weight == -0.17
    assert stable_card_projection(cards_after) == stable_card_projection(cards_before)
    assert {
        "entries": len(owner_session.exec(select(Entry)).all()),
        "feedback": len(owner_session.exec(select(ImportanceFeedbackEvent)).all()),
        "highlights": len(owner_session.exec(select(Highlight)).all()),
    } == counts_before
