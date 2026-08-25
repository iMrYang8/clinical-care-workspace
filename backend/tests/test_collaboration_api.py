from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.db import engine
from app.models import Comment, CommentMention


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
        headers=staff_headers,
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
    resolved = client.post(
        f"/api/v1/comments/{comment['id']}/resolve", headers=clinician_headers
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_at"] is not None

    assert (
        client.get(
            f"/api/v1/entries/{entry['id']}/comments", headers=auth_headers("patient")
        ).status_code
        == 403
    )
