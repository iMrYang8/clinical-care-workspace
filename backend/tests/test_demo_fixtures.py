from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.models import (
    AIRun,
    AuditEvent,
    CareTask,
    ClinicMembership,
    Comment,
    CommentMention,
    Entry,
    Highlight,
    ImportanceFeatureStat,
    ImportanceFeedbackEvent,
    PatientGlanceSnapshot,
    User,
)
from app.seed import demo_id, seed_demo_data
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

    assert owner_session.get(Comment, demo_id("comment-clinician-assignment"))
    assert owner_session.get(CommentMention, demo_id("comment-mention-clinician"))
    assert owner_session.get(CareTask, demo_id("task-fall-risk-review"))
    assert owner_session.get(AuditEvent, demo_id("audit-demo-fixture"))
    assert owner_session.get(
        ImportanceFeedbackEvent, demo_id("importance-feedback-medication")
    )


def test_reseed_preserves_feedback_and_rebuilds_snapshot_from_current_state(
    owner_session: Session,
) -> None:
    seed_demo_data(owner_session)
    sleep = owner_session.get(Highlight, demo_id("highlight-sleep"))
    feedback = owner_session.get(
        ImportanceFeedbackEvent, demo_id("importance-feedback-medication")
    )
    stat = owner_session.get(
        ImportanceFeatureStat, demo_id("importance-stat-medication")
    )
    clinician = owner_session.get(User, demo_id("user-clinician"))
    membership = owner_session.get(ClinicMembership, demo_id("membership-clinician"))
    assert sleep is not None and feedback is not None and stat is not None
    assert clinician is not None and membership is not None

    sleep.status = "rejected"
    sleep.pinned = False
    feedback.signal = "reject"
    feedback.applied_delta = -0.03
    stat.weight = -0.17
    owner_session.add_all([sleep, feedback, stat])
    rebuild_glance(
        owner_session,
        RequestContext(user=clinician, membership=membership),
        demo_id("patient-primary"),
    )
    owner_session.commit()

    snapshot = owner_session.get(PatientGlanceSnapshot, demo_id("glance-primary"))
    assert snapshot is not None
    cards_before, _ = read_glance(snapshot)
    assert str(sleep.id) not in {card["highlight_id"] for card in cards_before}
    counts_before = {
        "entries": len(owner_session.exec(select(Entry)).all()),
        "feedback": len(owner_session.exec(select(ImportanceFeedbackEvent)).all()),
        "highlights": len(owner_session.exec(select(Highlight)).all()),
    }

    seed_demo_data(owner_session)
    seed_demo_data(owner_session)
    owner_session.expire_all()

    preserved_sleep = owner_session.get(Highlight, sleep.id)
    preserved_feedback = owner_session.get(ImportanceFeedbackEvent, feedback.id)
    preserved_stat = owner_session.get(ImportanceFeatureStat, stat.id)
    snapshot = owner_session.get(PatientGlanceSnapshot, demo_id("glance-primary"))
    assert preserved_sleep is not None and preserved_feedback is not None
    assert preserved_stat is not None and snapshot is not None
    cards_after, _ = read_glance(snapshot)
    assert preserved_sleep.status == "rejected"
    assert preserved_sleep.pinned is False
    assert preserved_feedback.signal == "reject"
    assert preserved_feedback.applied_delta == -0.03
    assert preserved_stat.weight == -0.17
    assert cards_after == cards_before
    assert {
        "entries": len(owner_session.exec(select(Entry)).all()),
        "feedback": len(owner_session.exec(select(ImportanceFeedbackEvent)).all()),
        "highlights": len(owner_session.exec(select(Highlight)).all()),
    } == counts_before
