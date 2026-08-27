import hashlib
import math
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from app.api.deps import RequestContext
from app.core.db import engine, set_rls_clinic
from app.models import (
    AuditEvent,
    ClinicMembership,
    DomainEvent,
    Highlight,
    ImportanceFeatureStat,
    ImportanceFeedbackEvent,
    User,
)
from app.seed import demo_id
from app.services.importance import (
    MAX_FEATURE_WEIGHT,
    apply_weight_delta,
    calculate_score,
    record_feedback,
    sanitize_feature_keys,
)
from app.services.nightingale import rebuild_glance


@pytest.mark.unit
def test_feature_keys_are_non_phi_bounded_tokens() -> None:
    assert sanitize_feature_keys(
        [
            "entity:allergy",
            "topic:follow_up",
            "risk:critical",
            "patient:Tan Mei Ling",
            "free text secret",
        ]
    ) == ["entity:allergy", "topic:follow_up", "risk:critical"]
    bounded = [f"entry_type:type_{index}" for index in range(11)]
    assert sanitize_feature_keys(bounded) == bounded[:10]


@pytest.mark.unit
def test_clinic_feedback_math_is_bounded_and_diminishing() -> None:
    weight = 0.0
    first = apply_weight_delta(weight, 0.08, observations=0)
    second = apply_weight_delta(first, 0.08, observations=1)
    assert first == pytest.approx(0.08)
    assert second - first < first
    for observations in range(2, 1_000):
        weight = apply_weight_delta(weight, 0.08, observations=observations)
    assert weight == MAX_FEATURE_WEIGHT


@pytest.mark.unit
def test_protected_highlight_ignores_negative_learned_score() -> None:
    score = calculate_score(
        critical=True,
        unresolved=False,
        clinician_confirmed=False,
        feature_keys=["risk:critical"],
        feature_weights={"risk:critical": -0.2},
        age_days=0,
    )
    assert score.learned == 0
    assert score.final >= score.base
    assert math.isclose(score.final, score.base)

    learned = calculate_score(
        critical=False,
        unresolved=False,
        clinician_confirmed=False,
        feature_keys=["topic:follow_up"],
        feature_weights={"topic:follow_up": 0.1},
        age_days=0,
    )
    assert learned.risk_reason == "clinic_feedback"


def _create_allergy_highlight(
    client: TestClient, headers: dict[str, str], *, section: str
) -> dict:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": section,
            "title": "Synthetic allergy",
            "content": "allergy",
            "patient_facing": section == "clinician",
        },
    )
    assert entry.status_code == 201, entry.text
    response = client.post(
        f"/api/v1/entries/{entry.json()['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry.json()["version_id"],
            "start_offset": 0,
            "end_offset": 7,
            "exact_quote": "allergy",
            "label": "Allergy signal",
            "feature_keys": ["entity:allergy"],
            "patient_facing": section == "clinician",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_pin_learning_is_clinic_scoped_idempotent_clamped_and_patient_safe(
    client: TestClient, auth_headers
) -> None:
    clinic_a = auth_headers("clinician")
    clinic_b = auth_headers("other_staff")
    first_a = _create_allergy_highlight(client, clinic_a, section="clinician")
    _create_allergy_highlight(client, clinic_b, section="staff")

    pin_headers = clinic_a | {"Idempotency-Key": "pin-allergy-once"}
    for _ in range(2):
        response = client.post(
            f"/api/v1/highlights/{first_a['id']}/pin", headers=pin_headers
        )
        assert response.status_code == 200, response.text

    second_a = _create_allergy_highlight(client, clinic_a, section="clinician")
    second_b = _create_allergy_highlight(client, clinic_b, section="staff")
    assert second_a["learned_score"] > second_b["learned_score"]

    forged = client.post(
        f"/api/v1/highlights/{first_a['id']}/feedback",
        headers=clinic_a | {"Idempotency-Key": "forged-positive-signal"},
        json={"signal": "pin"},
    )
    assert forged.status_code == 422

    # Positive signals come only from their real state transitions; separate
    # synthetic highlights still drive the clinic statistic to its hard clamp.
    for index in range(5):
        candidate = _create_allergy_highlight(client, clinic_a, section="clinician")
        response = client.post(
            f"/api/v1/highlights/{candidate['id']}/pin",
            headers=clinic_a | {"Idempotency-Key": f"bounded-{index}"},
        )
        assert response.status_code == 200, response.text

    with Session(engine) as session:
        stats = session.exec(
            select(ImportanceFeatureStat).where(
                ImportanceFeatureStat.feature_key == "entity:allergy"
            )
        ).all()
        assert len(stats) == 1
        assert stats[0].weight == MAX_FEATURE_WEIGHT
        events = session.exec(
            select(ImportanceFeedbackEvent).where(
                ImportanceFeedbackEvent.highlight_id == uuid.UUID(first_a["id"]),
                ImportanceFeedbackEvent.signal == "pin",
                ImportanceFeedbackEvent.idempotency_key
                == hashlib.sha256(b"pin-allergy-once").hexdigest(),
            )
        ).all()
        assert len(events) == 1
        assert len(events[0].request_sha256) == 64

    with Session(engine) as session:
        set_rls_clinic(session, demo_id("clinic-other"))
        other_stats = session.exec(
            select(ImportanceFeatureStat).where(
                ImportanceFeatureStat.feature_key == "entity:allergy"
            )
        ).all()
        assert len(other_stats) == 1
        assert other_stats[0].weight < MAX_FEATURE_WEIGHT

    accepted = client.post(
        f"/api/v1/highlights/{first_a['id']}/accept", headers=clinic_a
    )
    assert accepted.status_code == 200
    patient_headers = auth_headers("patient")
    glance = client.get(
        f"/api/v1/patients/{first_a['patient_id']}/glance",
        headers=patient_headers,
    )
    assert glance.status_code == 200
    assert glance.json()["cards"]
    assert "score_components" not in glance.json()["cards"][0]


def test_feedback_idempotency_key_is_bound_to_target_and_signal(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    first = _create_allergy_highlight(client, headers, section="clinician")
    second = _create_allergy_highlight(client, headers, section="clinician")
    shared = headers | {"Idempotency-Key": "resource-bound-key"}

    accepted = client.post(f"/api/v1/highlights/{first['id']}/pin", headers=shared)
    assert accepted.status_code == 200, accepted.text
    already_pinned = client.post(
        f"/api/v1/highlights/{second['id']}/pin",
        headers=headers | {"Idempotency-Key": "second-resource-key"},
    )
    assert already_pinned.status_code == 200, already_pinned.text
    # Request binding is checked even though the second target is already in
    # the requested state; a no-op cannot bypass idempotency-key ownership.
    rejected = client.post(f"/api/v1/highlights/{second['id']}/pin", headers=shared)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    with Session(engine) as session:
        stored_second = session.get(Highlight, uuid.UUID(second["id"]))
        assert stored_second is not None
        assert stored_second.pinned is True
        events = session.exec(
            select(ImportanceFeedbackEvent).where(
                ImportanceFeedbackEvent.idempotency_key
                == hashlib.sha256(b"resource-bound-key").hexdigest()
            )
        ).all()
        assert len(events) == 1
        assert events[0].highlight_id == uuid.UUID(first["id"])
        assert events[0].signal == "pin"


def test_patient_is_denied_before_internal_highlight_lookup(
    client: TestClient, auth_headers
) -> None:
    clinician = auth_headers("clinician")
    patient = auth_headers("patient")
    highlight = _create_allergy_highlight(client, clinician, section="clinician")

    # Existing and random internal identifiers are intentionally
    # indistinguishable to a patient. Authorization happens before any lookup
    # or row lock, so neither endpoint becomes an existence oracle.
    for highlight_id in (highlight["id"], str(uuid.uuid4())):
        transitioned = client.post(
            f"/api/v1/highlights/{highlight_id}/pin",
            headers=patient | {"Idempotency-Key": f"patient-pin-{highlight_id}"},
        )
        feedback = client.post(
            f"/api/v1/highlights/{highlight_id}/feedback",
            headers=patient | {"Idempotency-Key": f"patient-dismiss-{highlight_id}"},
            json={"signal": "dismiss", "reason": "not_relevant"},
        )
        assert transitioned.status_code == 403
        assert feedback.status_code == 403
        assert (
            transitioned.json()
            == feedback.json()
            == {"detail": "Clinical review role required"}
        )


def test_dismiss_feedback_is_negative_idempotent_and_resource_bound(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    first = _create_allergy_highlight(client, headers, section="clinician")
    second = _create_allergy_highlight(client, headers, section="clinician")
    shared = headers | {"Idempotency-Key": "dismiss-resource-bound"}

    for _ in range(2):
        dismissed = client.post(
            f"/api/v1/highlights/{first['id']}/feedback",
            headers=shared,
            json={"signal": "dismiss", "reason": "not_relevant"},
        )
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["status"] == "dismissed"

    second_dismissed = client.post(
        f"/api/v1/highlights/{second['id']}/feedback",
        headers=headers | {"Idempotency-Key": "dismiss-second"},
        json={"signal": "dismiss", "reason": "not_relevant"},
    )
    assert second_dismissed.status_code == 200, second_dismissed.text
    conflict = client.post(
        f"/api/v1/highlights/{second['id']}/feedback",
        headers=shared,
        json={"signal": "dismiss", "reason": "not_relevant"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    with Session(engine) as session:
        feedback_events = session.exec(
            select(ImportanceFeedbackEvent).where(
                ImportanceFeedbackEvent.highlight_id == uuid.UUID(first["id"]),
                ImportanceFeedbackEvent.signal == "dismiss",
            )
        ).all()
        audits = session.exec(
            select(AuditEvent).where(
                    AuditEvent.resource_id == uuid.UUID(first["id"]),
                    AuditEvent.action
                    == "highlight.feedback.dismiss.not_relevant",
            )
        ).all()
        domain_events = session.exec(
            select(DomainEvent).where(
                    DomainEvent.aggregate_id == uuid.UUID(first["id"]),
                    DomainEvent.event_type
                    == "highlight.feedback.dismiss.not_relevant",
            )
        ).all()
        assert len(feedback_events) == 1
        assert feedback_events[0].applied_delta < 0
        assert len(audits) == 1
        assert len(domain_events) == 1


def test_stale_edit_feedback_refreshes_concurrent_pin_before_glance_rebuild(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    highlight = _create_allergy_highlight(client, headers, section="clinician")
    highlight_id = uuid.UUID(highlight["id"])

    with Session(engine) as stale_session:
        stale = stale_session.get(Highlight, highlight_id)
        user = stale_session.get(User, demo_id("user-clinician"))
        membership = stale_session.get(
            ClinicMembership, demo_id("membership-clinician")
        )
        assert stale is not None and user is not None and membership is not None
        assert stale.pinned is False

        pinned = client.post(
            f"/api/v1/highlights/{highlight['id']}/pin",
            headers=headers | {"Idempotency-Key": "concurrent-pin-before-edit"},
        )
        assert pinned.status_code == 200, pinned.text
        assert pinned.json()["pinned"] is True

        context = RequestContext(user=user, membership=membership)
        _, affected = record_feedback(
            stale_session,
            context,
            stale,
            signal="edit",
            idempotency_key="stale-edit-feedback",
        )
        assert stale.pinned is True
        for patient_id in affected | {stale.patient_id}:
            rebuild_glance(stale_session, context, patient_id)
        stale_session.commit()

    glance = client.get(
        f"/api/v1/patients/{highlight['patient_id']}/glance", headers=headers
    )
    assert glance.status_code == 200, glance.text
    card = next(
        item
        for item in glance.json()["cards"]
        if item["highlight_id"] == highlight["id"]
    )
    assert card["pinned"] is True


def test_concurrent_first_feature_and_same_request_feedback_are_atomic(
    client: TestClient, auth_headers, owner_session: Session
) -> None:
    headers = auth_headers("clinician")
    first = _create_allergy_highlight(client, headers, section="clinician")
    second = _create_allergy_highlight(client, headers, section="clinician")

    # Highlight creation intentionally learns a manual signal. Remove that
    # owner-side fixture state so the concurrent pin requests race on the first
    # feature-stat observation.
    owner_session.exec(delete(ImportanceFeedbackEvent))
    owner_session.exec(delete(ImportanceFeatureStat))
    owner_session.commit()

    barrier = Barrier(2)

    def pin(highlight_id: str, key: str) -> int:
        barrier.wait()
        return client.post(
            f"/api/v1/highlights/{highlight_id}/pin",
            headers=headers | {"Idempotency-Key": key},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = [
            future.result()
            for future in (
                pool.submit(pin, first["id"], "concurrent-first-a"),
                pool.submit(pin, second["id"], "concurrent-first-b"),
            )
        ]
    assert statuses == [200, 200]
    with Session(engine) as session:
        stat = session.exec(
            select(ImportanceFeatureStat).where(
                ImportanceFeatureStat.feature_key == "entity:allergy"
            )
        ).one()
        assert stat.observation_count == 2

    third = _create_allergy_highlight(client, headers, section="clinician")
    replay_barrier = Barrier(2)

    def replay() -> int:
        replay_barrier.wait()
        return client.post(
            f"/api/v1/highlights/{third['id']}/pin",
            headers=headers | {"Idempotency-Key": "concurrent-same-request"},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        replay_statuses = [
            future.result() for future in (pool.submit(replay), pool.submit(replay))
        ]
    assert replay_statuses == [200, 200]
    with Session(engine) as session:
        events = session.exec(
            select(ImportanceFeedbackEvent).where(
                ImportanceFeedbackEvent.idempotency_key
                == hashlib.sha256(b"concurrent-same-request").hexdigest()
            )
        ).all()
        assert len(events) == 1
        audits = session.exec(
            select(AuditEvent).where(
                AuditEvent.resource_id == uuid.UUID(third["id"]),
                AuditEvent.action == "highlight.pin",
            )
        ).all()
        domain_events = session.exec(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == uuid.UUID(third["id"]),
                DomainEvent.event_type == "highlight.pin",
            )
        ).all()
        assert len(audits) == 1
        assert len(domain_events) == 1

    fourth = _create_allergy_highlight(client, headers, section="clinician")
    transition_barrier = Barrier(2)

    def distinct_request(key: str) -> int:
        transition_barrier.wait()
        return client.post(
            f"/api/v1/highlights/{fourth['id']}/pin",
            headers=headers | {"Idempotency-Key": key},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        distinct_statuses = [
            future.result()
            for future in (
                pool.submit(distinct_request, "same-transition-a"),
                pool.submit(distinct_request, "same-transition-b"),
            )
        ]
    assert distinct_statuses == [200, 200]
    with Session(engine) as session:
        events = session.exec(
            select(ImportanceFeedbackEvent).where(
                ImportanceFeedbackEvent.highlight_id == uuid.UUID(fourth["id"]),
                ImportanceFeedbackEvent.signal == "pin",
            )
        ).all()
        audits = session.exec(
            select(AuditEvent).where(
                AuditEvent.resource_id == uuid.UUID(fourth["id"]),
                AuditEvent.action == "highlight.pin",
            )
        ).all()
        domain_events = session.exec(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == uuid.UUID(fourth["id"]),
                DomainEvent.event_type == "highlight.pin",
            )
        ).all()
        assert len(events) == 1
        assert len(audits) == 1
        assert len(domain_events) == 1
