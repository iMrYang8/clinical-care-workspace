from fastapi.testclient import TestClient


def _entry(client: TestClient, headers: dict[str, str]) -> dict:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Evidence",
            "content": "prefix IMPORTANT suffix",
            "patient_facing": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_provenance_resolves_against_immutable_version_after_edit(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    entry = _entry(client, headers)
    highlight = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Medication risk",
            "critical": True,
            "patient_facing": True,
        },
    )
    assert highlight.status_code == 201, highlight.text
    pointer_id = highlight.json()["provenance_pointer_id"]
    assert highlight.json()["anchor_state"] == "resolved"

    changed = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=headers | {"If-Match": entry["version_id"]},
        json={"content": "the current version no longer contains that quote"},
    )
    assert changed.status_code == 200, changed.text

    resolved = client.get(f"/api/v1/provenance/{pointer_id}/resolve", headers=headers)
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["state"] == "resolved"
    assert body["exact_quote"] == "IMPORTANT"
    assert body["entry_version_id"] == entry["version_id"]


def test_invalid_anchor_is_explicitly_orphaned_and_never_guessed(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    entry = _entry(client, headers)
    response = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": 9,
            "exact_quote": "IMPORTANT",
            "prefix": "",
            "suffix": " suffix",
            "label": "Broken anchor",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["anchor_state"] == "orphaned"
    assert response.json()["review_required"] is True

    pointer_id = response.json()["provenance_pointer_id"]
    resolved = client.get(f"/api/v1/provenance/{pointer_id}/resolve", headers=headers)
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "orphaned"
    assert resolved.json()["review_required"] is True

    for transition in ("accept", "pin"):
        promoted = client.post(
            f"/api/v1/highlights/{response.json()['id']}/{transition}",
            headers=headers,
        )
        assert promoted.status_code == 409
        assert promoted.json()["detail"]["code"] == "PROVENANCE_REVIEW_REQUIRED"


def test_internal_version_cannot_be_republished_through_provenance(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    internal = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Internal evidence",
            "content": "prefix PRIVATE suffix",
            "patient_facing": False,
        },
    ).json()
    published = client.patch(
        f"/api/v1/entries/{internal['id']}",
        headers=headers | {"If-Match": internal["version_id"]},
        json={"patient_facing": True},
    )
    assert published.status_code == 200, published.text

    leaked = client.post(
        f"/api/v1/entries/{internal['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": internal["version_id"],
            "start_offset": 7,
            "end_offset": 14,
            "exact_quote": "PRIVATE",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Must remain internal",
            "patient_facing": True,
        },
    )
    assert leaked.status_code == 409
    assert leaked.json()["detail"]["code"] == "SOURCE_NOT_PATIENT_FACING"


def test_patient_only_resolves_reviewed_patient_facing_provenance(
    client: TestClient, auth_headers
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_headers = auth_headers("patient")
    entry = _entry(client, clinician_headers)
    created = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=clinician_headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Reviewed source",
            "patient_facing": True,
        },
    )
    assert created.status_code == 201, created.text
    pointer_id = created.json()["provenance_pointer_id"]
    assert (
        client.get(
            f"/api/v1/provenance/{pointer_id}/resolve", headers=patient_headers
        ).status_code
        == 404
    )

    accepted = client.post(
        f"/api/v1/highlights/{created.json()['id']}/accept",
        headers=clinician_headers,
    )
    assert accepted.status_code == 200, accepted.text
    visible = client.get(
        f"/api/v1/provenance/{pointer_id}/resolve", headers=patient_headers
    )
    assert visible.status_code == 200, visible.text
    assert visible.json()["entry_version_id"] == entry["version_id"]


def test_withdrawing_entry_removes_accepted_highlight_from_patient_glance(
    client: TestClient, auth_headers
) -> None:
    """A cached card must not outlive the current entry's sharing decision."""

    clinician_headers = auth_headers("clinician")
    patient_headers = auth_headers("patient")
    entry = _entry(client, clinician_headers)
    created = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=clinician_headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "WITHDRAW-ME",
            "patient_facing": True,
        },
    )
    assert created.status_code == 201, created.text
    pointer_id = created.json()["provenance_pointer_id"]
    accepted = client.post(
        f"/api/v1/highlights/{created.json()['id']}/accept",
        headers=clinician_headers,
    )
    assert accepted.status_code == 200, accepted.text

    before = client.get(
        f"/api/v1/patients/{entry['patient_id']}/glance",
        headers=patient_headers,
    )
    assert before.status_code == 200, before.text
    assert "WITHDRAW-ME" in {card["label"] for card in before.json()["cards"]}
    assert (
        client.get(
            f"/api/v1/provenance/{pointer_id}/resolve", headers=patient_headers
        ).status_code
        == 200
    )

    withdrawn = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=clinician_headers | {"If-Match": entry["version_id"]},
        json={"patient_facing": False},
    )
    assert withdrawn.status_code == 200, withdrawn.text

    after = client.get(
        f"/api/v1/patients/{entry['patient_id']}/glance",
        headers=patient_headers,
    )
    assert after.status_code == 200, after.text
    assert "WITHDRAW-ME" not in {card["label"] for card in after.json()["cards"]}
    assert (
        client.get(
            f"/api/v1/provenance/{pointer_id}/resolve", headers=patient_headers
        ).status_code
        == 404
    )
    timeline = client.get(
        f"/api/v1/patients/{entry['patient_id']}/timeline",
        headers=patient_headers,
    )
    assert entry["id"] not in {row["id"] for row in timeline.json()["data"]}


def test_accept_and_pin_rebuild_precomputed_glance_with_max_five_cards(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("clinician")
    entry = _entry(client, headers)
    patient_id = entry["patient_id"]
    created: list[str] = []
    for index in range(7):
        response = client.post(
            f"/api/v1/entries/{entry['id']}/highlights",
            headers=headers,
            json={
                "entry_version_id": entry["version_id"],
                "start_offset": 7,
                "end_offset": 16,
                "exact_quote": "IMPORTANT",
                "prefix": "prefix ",
                "suffix": " suffix",
                "label": f"Card {index}",
                "critical": index == 0,
                "patient_facing": True,
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
        accepted = client.post(
            f"/api/v1/highlights/{created[-1]}/accept", headers=headers
        )
        assert accepted.status_code == 200
    pinned = client.post(f"/api/v1/highlights/{created[-1]}/pin", headers=headers)
    assert pinned.status_code == 200

    glance = client.get(f"/api/v1/patients/{patient_id}/glance", headers=headers)
    assert glance.status_code == 200, glance.text
    assert len(glance.json()["cards"]) == 5
    assert glance.json()["source"] == "precomputed"


def test_internal_top_five_cannot_crowd_out_patient_eligible_cards(
    client: TestClient, auth_headers
) -> None:
    clinician_headers = auth_headers("clinician")
    patient_headers = auth_headers("patient")
    patient_id = client.get("/api/v1/patients", headers=clinician_headers).json()[
        "data"
    ][0]["id"]

    def create_entry(title: str, *, patient_facing: bool) -> dict:
        response = client.post(
            "/api/v1/entries",
            headers=clinician_headers,
            json={
                "patient_id": patient_id,
                "section": "clinician",
                "title": title,
                "content": "prefix IMPORTANT suffix",
                "patient_facing": patient_facing,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def create_pinned(entry: dict, label: str, *, patient_facing: bool) -> None:
        created = client.post(
            f"/api/v1/entries/{entry['id']}/highlights",
            headers=clinician_headers,
            json={
                "entry_version_id": entry["version_id"],
                "start_offset": 7,
                "end_offset": 16,
                "exact_quote": "IMPORTANT",
                "prefix": "prefix ",
                "suffix": " suffix",
                "label": label,
                "critical": True,
                "patient_facing": patient_facing,
            },
        )
        assert created.status_code == 201, created.text
        pinned = client.post(
            f"/api/v1/highlights/{created.json()['id']}/pin",
            headers=clinician_headers,
        )
        assert pinned.status_code == 200, pinned.text

    public_entry = create_entry("Public source", patient_facing=True)
    for index in range(5):
        create_pinned(public_entry, f"PUBLIC-CARD-{index}", patient_facing=True)
    internal_entry = create_entry("Internal source", patient_facing=False)
    for index in range(5):
        create_pinned(internal_entry, f"INTERNAL-CARD-{index}", patient_facing=False)

    clinical = client.get(
        f"/api/v1/patients/{patient_id}/glance", headers=clinician_headers
    )
    assert clinical.status_code == 200, clinical.text
    assert {card["label"] for card in clinical.json()["cards"]} == {
        f"INTERNAL-CARD-{index}" for index in range(5)
    }

    patient = client.get(
        f"/api/v1/patients/{patient_id}/glance", headers=patient_headers
    )
    assert patient.status_code == 200, patient.text
    assert {card["label"] for card in patient.json()["cards"]} == {
        f"PUBLIC-CARD-{index}" for index in range(5)
    }
    for card in patient.json()["cards"]:
        assert set(card) == {
            "highlight_id",
            "label",
            "provenance_pointer_id",
        }
