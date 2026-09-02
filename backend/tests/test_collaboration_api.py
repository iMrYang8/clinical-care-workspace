import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from app.api.routes import collaboration as collaboration_route
from app.api.routes import events as events_route
from app.core.db import engine
from app.models import AuditEvent, Comment, CommentMention, DomainEvent


def test_comment_creation_requires_current_entry_etag(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("staff")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Comment race",
            "content": "anchored",
        },
    ).json()
    body = {
        "entry_version_id": entry["version_id"],
        "start_offset": 0,
        "end_offset": 8,
        "exact_quote": "anchored",
        "body": "Review this source.",
    }
    missing = client.post(
        f"/api/v1/entries/{entry['id']}/comments", headers=headers, json=body
    )
    assert missing.status_code == 428

    updated = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=headers | {"If-Match": entry["version_id"]},
        json={"content": "anchored updated"},
    )
    assert updated.status_code == 200, updated.text
    stale = client.post(
        f"/api/v1/entries/{entry['id']}/comments",
        headers=headers | {"If-Match": entry["version_id"]},
        json=body,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "VERSION_CONFLICT"


def test_comment_anchor_mentions_assignment_resolve_and_encryption(
    client: TestClient, auth_headers
) -> None:
    staff_headers = auth_headers("staff")
    clinician_headers = auth_headers("clinician")
    clinician = client.get("/api/v1/auth/me", headers=clinician_headers).json()
    patient_id = client.get("/api/v1/patients", headers=staff_headers).json()["data"][
        0
    ]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Collaboration",
            "content": "prefix discuss-this suffix",
        },
    ).json()
    created = client.post(
        f"/api/v1/entries/{entry['id']}/comments",
        headers=staff_headers | {"If-Match": entry["version_id"]},
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 7,
            "end_offset": 19,
            "exact_quote": "discuss-this",
            "prefix": "prefix ",
            "suffix": " suffix",
            "body": "Please review the synthetic care note.",
            "mentioned_user_ids": [clinician["user_id"]],
            "assigned_membership_id": clinician["membership_id"],
        },
    )
    assert created.status_code == 201, created.text
    comment = created.json()
    assert comment["anchor_state"] == "resolved"
    assert comment["assigned_membership_id"] == clinician["membership_id"]
    assert comment["mentioned_user_ids"] == [clinician["user_id"]]

    with Session(engine) as session:
        stored = session.get(Comment, comment["id"])
        assert stored is not None
        assert b"Please review" not in stored.body_ciphertext
        mention = session.exec(
            select(CommentMention).where(CommentMention.comment_id == stored.id)
        ).one()
        assert str(mention.mentioned_user_id) == clinician["user_id"]

    listed = client.get(
        f"/api/v1/entries/{entry['id']}/comments", headers=clinician_headers
    )
    assert listed.status_code == 200
    assert listed.json()[0]["body"] == "Please review the synthetic care note."
    assert listed.json()[0]["mentioned_user_ids"] == [clinician["user_id"]]
    missing_resolution_etag = client.post(
        f"/api/v1/comments/{comment['id']}/resolve", headers=clinician_headers
    )
    assert missing_resolution_etag.status_code == 428
    assert missing_resolution_etag.headers["etag"] == f'"{comment["revision"]}"'
    resolved = client.post(
        f"/api/v1/comments/{comment['id']}/resolve",
        headers=clinician_headers | {"If-Match": str(comment["revision"])},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_at"] is not None
    assert resolved.json()["revision"] == comment["revision"] + 1
    assert resolved.headers["etag"] == f'"{resolved.json()["revision"]}"'

    assert (
        client.post(
            f"/api/v1/comments/{comment['id']}/unresolve",
            headers=auth_headers("admin")
            | {"If-Match": str(resolved.json()["revision"])},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/comments/{comment['id']}/unresolve",
            headers=auth_headers("patient")
            | {"If-Match": str(resolved.json()["revision"])},
        ).status_code
        == 403
    )
    reopened = client.post(
        f"/api/v1/comments/{comment['id']}/unresolve",
        headers=clinician_headers | {"If-Match": str(resolved.json()["revision"])},
    )
    assert reopened.status_code == 200
    assert reopened.json()["resolved_at"] is None
    assert reopened.json()["revision"] == resolved.json()["revision"] + 1
    assert reopened.headers["etag"] == f'"{reopened.json()["revision"]}"'

    with Session(engine) as session:
        comment_id = uuid.UUID(comment["id"])
        audit = session.exec(
            select(AuditEvent).where(
                AuditEvent.resource_id == comment_id,
                AuditEvent.action == "comment.unresolved",
            )
        ).one()
        domain_event = session.exec(
            select(DomainEvent).where(
                DomainEvent.aggregate_id == comment_id,
                DomainEvent.event_type == "comment.unresolved",
            )
        ).one()
        assert audit.metadata_json["entry_id"] == entry["id"]
        assert domain_event.payload_json["entry_id"] == entry["id"]

    patient_membership = client.get(
        "/api/v1/auth/me", headers=auth_headers("patient")
    ).json()["membership_id"]
    invalid_assignment = client.patch(
        f"/api/v1/comments/{comment['id']}/assignment",
        headers=staff_headers | {"If-Match": str(reopened.json()["revision"])},
        json={"assigned_membership_id": patient_membership},
    )
    assert invalid_assignment.status_code == 422

    missing_assignment_etag = client.patch(
        f"/api/v1/comments/{comment['id']}/assignment",
        headers=staff_headers,
        json={"assigned_membership_id": None},
    )
    assert missing_assignment_etag.status_code == 428
    assert missing_assignment_etag.headers["etag"] == (
        f'"{reopened.json()["revision"]}"'
    )

    reassigned = client.patch(
        f"/api/v1/comments/{comment['id']}/assignment",
        headers=staff_headers | {"If-Match": str(reopened.json()["revision"])},
        json={"assigned_membership_id": None},
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["revision"] == reopened.json()["revision"] + 1
    assert reassigned.headers["etag"] == f'"{reassigned.json()["revision"]}"'
    stale_assignment = client.patch(
        f"/api/v1/comments/{comment['id']}/assignment",
        headers=staff_headers | {"If-Match": str(reopened.json()["revision"])},
        json={"assigned_membership_id": clinician["membership_id"]},
    )
    assert stale_assignment.status_code == 409, stale_assignment.text
    assert stale_assignment.json()["detail"] == {
        "code": "COMMENT_VERSION_CONFLICT",
        "latest_revision": reassigned.json()["revision"],
    }

    assert (
        client.get(
            f"/api/v1/entries/{entry['id']}/comments", headers=auth_headers("patient")
        ).status_code
        == 403
    )


def test_editor_presence_is_scoped_content_free_and_expires(
    client: TestClient, auth_headers, monkeypatch
) -> None:
    staff_headers = auth_headers("staff")
    patient_id = client.get("/api/v1/patients", headers=staff_headers).json()["data"][
        0
    ]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Presence fixture",
            "content": "Synthetic content that must never enter presence.",
        },
    ).json()
    other_entry = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Other presence fixture",
            "content": "Unrelated synthetic content.",
        },
    ).json()
    frozen = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)
    monkeypatch.setattr(collaboration_route, "get_datetime_utc", lambda: frozen)

    rejects_draft = client.post(
        f"/api/v1/entries/{entry['id']}/presence",
        headers=staff_headers,
        json={
            "entry_version_id": entry["version_id"],
            "draft_content": "PHI-CANARY-MUST-NOT-BE-ACCEPTED",
        },
    )
    assert rejects_draft.status_code == 422
    wrong_version = client.post(
        f"/api/v1/entries/{entry['id']}/presence",
        headers=staff_headers,
        json={"entry_version_id": other_entry["version_id"]},
    )
    assert wrong_version.status_code == 404
    assert (
        client.post(
            f"/api/v1/entries/{entry['id']}/presence",
            headers=auth_headers("patient"),
            json={"entry_version_id": entry["version_id"]},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/entries/{entry['id']}/presence",
            headers=auth_headers("other_staff"),
            json={"entry_version_id": entry["version_id"]},
        ).status_code
        == 404
    )

    response = client.post(
        f"/api/v1/entries/{entry['id']}/presence",
        headers=staff_headers,
        json={"entry_version_id": entry["version_id"]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["patient_id"] == patient_id
    assert payload["entry_id"] == entry["id"]
    assert payload["entry_version_id"] == entry["version_id"]
    assert payload["actor_role"] == "staff"
    assert datetime.fromisoformat(payload["expires_at"]) == frozen + timedelta(
        seconds=collaboration_route.EDITOR_PRESENCE_TTL_SECONDS
    )
    assert set(payload) == {
        "clinic_id",
        "patient_id",
        "entry_id",
        "entry_version_id",
        "actor_id",
        "actor_role",
        "actor_display_name",
        "expires_at",
    }

    with Session(engine) as session:
        event = session.exec(
            select(DomainEvent)
            .where(
                DomainEvent.event_type == "editor_presence",
                DomainEvent.aggregate_id == uuid.UUID(entry["id"]),
            )
            .order_by(col(DomainEvent.sequence_no).desc())
        ).first()
        assert event is not None
        assert event.aggregate_type == "entry"
        assert event.payload_json == payload
        assert "PHI-CANARY" not in events_route._event_frame(event)
        assert events_route._presence_event_is_live(
            event, now_epoch=(frozen + timedelta(seconds=44)).timestamp()
        )
        assert not events_route._presence_event_is_live(
            event, now_epoch=(frozen + timedelta(seconds=45)).timestamp()
        )
        event.payload_json = {**event.payload_json, "expires_at": "not-a-timestamp"}
        assert not events_route._presence_event_is_live(
            event, now_epoch=frozen.timestamp()
        )


def test_assignment_if_match_serializes_racing_updates(
    client: TestClient, auth_headers
) -> None:
    staff_headers = auth_headers("staff")
    clinician = client.get("/api/v1/auth/me", headers=auth_headers("clinician")).json()
    patient_id = client.get("/api/v1/patients", headers=staff_headers).json()["data"][
        0
    ]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Assignment race",
            "content": "race-anchor",
        },
    ).json()
    comment = client.post(
        f"/api/v1/entries/{entry['id']}/comments",
        headers=staff_headers | {"If-Match": entry["version_id"]},
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": 11,
            "exact_quote": "race-anchor",
            "body": "Race this assignment.",
        },
    ).json()
    shared_etag = str(comment["revision"])
    barrier = Barrier(2)

    def update(assigned_membership_id: str | None):
        barrier.wait(timeout=5)
        return client.patch(
            f"/api/v1/comments/{comment['id']}/assignment",
            headers=staff_headers | {"If-Match": shared_etag},
            json={"assigned_membership_id": assigned_membership_id},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, [None, clinician["membership_id"]]))

    assert sorted(response.status_code for response in outcomes) == [200, 409]
    winner = next(response for response in outcomes if response.status_code == 200)
    loser = next(response for response in outcomes if response.status_code == 409)
    assert winner.json()["revision"] == comment["revision"] + 1
    assert loser.json()["detail"] == {
        "code": "COMMENT_VERSION_CONFLICT",
        "latest_revision": winner.json()["revision"],
    }


def test_resolution_if_match_serializes_race_and_detects_second_mutation_after_load(
    client: TestClient, auth_headers
) -> None:
    staff_headers = auth_headers("staff")
    clinician_headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=staff_headers).json()["data"][
        0
    ]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=staff_headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Resolution race",
            "content": "resolution-anchor",
        },
    ).json()
    comment = client.post(
        f"/api/v1/entries/{entry['id']}/comments",
        headers=staff_headers | {"If-Match": entry["version_id"]},
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": 17,
            "exact_quote": "resolution-anchor",
            "body": "Race this resolution.",
        },
    ).json()
    shared_etag = str(comment["revision"])
    barrier = Barrier(2)

    def resolve_once(_index: int):
        barrier.wait(timeout=5)
        return client.post(
            f"/api/v1/comments/{comment['id']}/resolve",
            headers=clinician_headers | {"If-Match": shared_etag},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(resolve_once, range(2)))

    assert sorted(response.status_code for response in outcomes) == [200, 409]
    winner = next(response for response in outcomes if response.status_code == 200)
    loser = next(response for response in outcomes if response.status_code == 409)
    assert winner.json()["revision"] == comment["revision"] + 1
    assert winner.headers["etag"] == f'"{winner.json()["revision"]}"'
    assert loser.json()["detail"] == {
        "code": "COMMENT_VERSION_CONFLICT",
        "latest_revision": winner.json()["revision"],
    }
    assert loser.headers["etag"] == winner.headers["etag"]

    # Editor A loads the latest revision, then editor B mutates it before A's
    # next state change. The second mutation must surface another 409 rather
    # than silently applying against A's stale snapshot.
    loaded = client.get(
        f"/api/v1/entries/{entry['id']}/comments", headers=staff_headers
    ).json()[0]
    assert loaded["revision"] == winner.json()["revision"]
    competing = client.post(
        f"/api/v1/comments/{comment['id']}/unresolve",
        headers=clinician_headers | {"If-Match": str(loaded["revision"])},
    )
    assert competing.status_code == 200, competing.text
    assert competing.json()["revision"] == loaded["revision"] + 1

    second_conflict = client.post(
        f"/api/v1/comments/{comment['id']}/resolve",
        headers=staff_headers | {"If-Match": str(loaded["revision"])},
    )
    assert second_conflict.status_code == 409, second_conflict.text
    assert (
        second_conflict.json()["detail"]["latest_revision"]
        == competing.json()["revision"]
    )
    assert second_conflict.headers["etag"] == f'"{competing.json()["revision"]}"'

    # A no-op request with the current revision is idempotent: no phantom
    # revision is created when the requested state already holds.
    idempotent = client.post(
        f"/api/v1/comments/{comment['id']}/unresolve",
        headers=staff_headers | {"If-Match": str(competing.json()["revision"])},
    )
    assert idempotent.status_code == 200, idempotent.text
    assert idempotent.json()["revision"] == competing.json()["revision"]
    assert idempotent.headers["etag"] == f'"{competing.json()["revision"]}"'
