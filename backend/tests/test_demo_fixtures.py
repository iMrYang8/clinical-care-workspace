from sqlmodel import Session, select

from app.models import (
    AuditEvent,
    CareTask,
    Comment,
    CommentMention,
    Entry,
    ImportanceFeedbackEvent,
    PatientGlanceSnapshot,
)
from app.seed import demo_id, seed_demo_data
from app.services.nightingale import read_glance


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
