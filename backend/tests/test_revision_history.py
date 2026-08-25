from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.db import engine
from app.models import AuditEvent, EntryRelation


def _make_entry(client: TestClient, headers: dict[str, str]) -> dict:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Versioned note",
            "content": "version one",
            "patient_facing": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_patch_requires_if_match_and_stale_writes_conflict(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("staff")
    entry = _make_entry(client, headers)
    url = f"/api/v1/entries/{entry['id']}"
    assert (
        client.patch(url, headers=headers, json={"content": "no cas"}).status_code
        == 428
    )

    first = client.patch(
        url,
        headers=headers | {"If-Match": entry["version_id"]},
        json={"content": "version two"},
    )
    assert first.status_code == 200, first.text
    stale = client.patch(
        url,
        headers=headers | {"If-Match": entry["version_id"]},
        json={"content": "lost update"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "VERSION_CONFLICT"


def test_versions_diff_and_revert_create_immutable_history(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("staff")
    entry = _make_entry(client, headers)
    url = f"/api/v1/entries/{entry['id']}"
    second = client.patch(
        url,
        headers=headers | {"If-Match": entry["version_id"]},
        json={"content": "version two"},
    ).json()

    versions = client.get(f"{url}/versions", headers=headers)
    assert versions.status_code == 200
    assert [v["version_no"] for v in versions.json()["data"]] == [1, 2]
    first_version = versions.json()["data"][0]
    diff = client.get(
        f"{url}/versions/{first_version['id']}/diff",
        params={"against": second["version_id"]},
        headers=headers,
    )
    assert diff.status_code == 200
    assert "-version one" in diff.json()["unified_diff"]
    assert "+version two" in diff.json()["unified_diff"]

    reverted = client.post(
        f"{url}/versions/{first_version['id']}/revert",
        headers=headers | {"If-Match": second["version_id"]},
    )
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["content"] == "version one"
    after = client.get(f"{url}/versions", headers=headers).json()["data"]
    assert len(after) == 3
    assert after[-1]["reverted_from_version_id"] == first_version["id"]

    with Session(engine) as session:
        audit = session.exec(
            select(AuditEvent).where(AuditEvent.resource_id == entry["id"])
        ).all()
    revert_events = [event for event in audit if event.action == "entry.reverted"]
    assert len(revert_events) == 1
    assert (
        revert_events[0].metadata_json["reverted_from_version_id"]
        == first_version["id"]
    )


def test_ai_entry_is_immutable_and_clinician_correction_supersedes_it(
    client: TestClient, auth_headers
) -> None:
    worker_headers = auth_headers("worker")
    clinician_headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician_headers).json()[
        "data"
    ][0]["id"]
    ai_entry = client.post(
        "/api/v1/entries",
        headers=worker_headers,
        json={
            "patient_id": patient_id,
            "section": "system",
            "origin": "ai",
            "title": "Synthetic AI extraction",
            "content": "Unreviewed generated fact",
        },
    )
    assert ai_entry.status_code == 201, ai_entry.text
    ai = ai_entry.json()
    immutable = client.patch(
        f"/api/v1/entries/{ai['id']}",
        headers=clinician_headers | {"If-Match": ai["version_id"]},
        json={"content": "overwrite"},
    )
    assert immutable.status_code == 409
    assert immutable.json()["detail"]["code"] == "AI_ENTRY_IMMUTABLE"

    correction = client.post(
        "/api/v1/entries",
        headers=clinician_headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Clinician correction",
            "content": "Reviewed correction",
            "supersedes_entry_id": ai["id"],
        },
    )
    assert correction.status_code == 201, correction.text
    with Session(engine) as session:
        relation = session.exec(
            select(EntryRelation).where(
                EntryRelation.source_entry_id == correction.json()["id"]
            )
        ).one()
    assert str(relation.target_entry_id) == ai["id"]
    assert relation.relation_type == "supersedes"
