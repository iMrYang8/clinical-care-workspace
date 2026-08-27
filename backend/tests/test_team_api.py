from sqlmodel import Session

from app.models import ClinicMembership
from app.seed import demo_id


def test_team_directory_is_minimal_active_and_clinic_scoped(
    client, auth_headers, owner_session: Session
) -> None:
    staff = auth_headers("staff")
    response = client.get("/api/v1/team/members", headers=staff)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 3
    assert {member["role"] for member in body["data"]} == {
        "staff",
        "clinician",
        "admin",
    }
    assert all(
        set(member) == {"membership_id", "user_id", "full_name", "role"}
        for member in body["data"]
    )
    assert str(demo_id("membership-other_staff")) not in {
        member["membership_id"] for member in body["data"]
    }

    clinician = owner_session.get(ClinicMembership, demo_id("membership-clinician"))
    assert clinician is not None
    clinician.is_active = False
    owner_session.add(clinician)
    owner_session.commit()
    after_deactivation = client.get("/api/v1/team/members", headers=staff)
    assert after_deactivation.status_code == 200
    assert str(clinician.id) not in {
        member["membership_id"] for member in after_deactivation.json()["data"]
    }


def test_team_directory_rejects_patient_and_worker(client, auth_headers) -> None:
    for persona in ("patient", "worker"):
        response = client.get("/api/v1/team/members", headers=auth_headers(persona))
        assert response.status_code == 403
