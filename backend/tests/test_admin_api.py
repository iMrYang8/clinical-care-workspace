from fastapi.testclient import TestClient

from app.seed import demo_id


def test_admin_manages_only_clinic_membership_metadata(
    client: TestClient, auth_headers
) -> None:
    admin = auth_headers("admin")
    listed = client.get("/api/v1/admin/memberships", headers=admin)
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 5
    assert all("hashed_password" not in item for item in listed.json()["data"])

    created = client.post(
        "/api/v1/admin/memberships",
        headers=admin,
        json={
            "email": "invited-clinician@nightingale.synthetic",
            "full_name": "Invited Synthetic Clinician",
            "role": "clinician",
            "temporary_password": "synthetic-temporary-only",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "clinician"
    assert created.json()["is_active"] is True

    duplicate = client.post(
        "/api/v1/admin/memberships",
        headers=admin,
        json={
            "email": "invited-clinician@nightingale.synthetic",
            "role": "staff",
            "temporary_password": "synthetic-temporary-only",
        },
    )
    assert duplicate.status_code == 409

    deactivated = client.post(
        f"/api/v1/admin/memberships/{created.json()['id']}/deactivate",
        headers=admin,
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    assert (
        client.post(
            f"/api/v1/admin/memberships/{demo_id('membership-admin')}/deactivate",
            headers=admin,
        ).status_code
        == 409
    )


def test_admin_audit_is_metadata_only_and_cross_clinic_hidden(
    client: TestClient, auth_headers
) -> None:
    admin = auth_headers("admin")
    response = client.get("/api/v1/admin/audit", headers=admin)
    assert response.status_code == 200, response.text
    forbidden = {
        "content",
        "title",
        "body",
        "metadata_json",
        "raw_ai",
        "patient_name",
    }
    for event in response.json()["data"]:
        assert forbidden.isdisjoint(event)
        assert set(event) == {
            "id",
            "actor_id",
            "action",
            "resource_type",
            "resource_id",
            "version_id",
            "created_at",
        }

    assert (
        client.get(
            "/api/v1/admin/memberships", headers=auth_headers("staff")
        ).status_code
        == 403
    )
    assert (
        client.get("/api/v1/admin/audit", headers=auth_headers("staff")).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/admin/memberships/{demo_id('membership-other_staff')}/deactivate",
            headers=admin,
        ).status_code
        == 404
    )
