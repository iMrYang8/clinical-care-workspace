from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient


def _create(client: TestClient, headers: dict[str, str], title: str) -> dict:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": title,
            "content": "initial",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _patch(
    client: TestClient, headers: dict[str, str], entry: dict, content: str
) -> int:
    response = client.patch(
        f"/api/v1/entries/{entry['id']}",
        headers=headers | {"If-Match": entry["version_id"]},
        json={"content": content},
    )
    return response.status_code


def test_same_entry_has_one_success_and_one_deterministic_409(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("staff")
    entry = _create(client, headers, "same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(
                lambda content: _patch(client, headers, entry, content),
                ["writer-a", "writer-b"],
            )
        )
    assert sorted(statuses) == [200, 409]


def test_different_entries_can_be_updated_independently(
    client: TestClient, auth_headers
) -> None:
    headers = auth_headers("staff")
    entries = [_create(client, headers, "left"), _create(client, headers, "right")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(
                lambda pair: _patch(client, headers, pair[0], pair[1]),
                zip(entries, ["left changed", "right changed"], strict=True),
            )
        )
    assert statuses == [200, 200]
