from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import col, select

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models import ClinicInvitation, ClinicMembership, User
from app.seed import demo_id


def _capture_invitation(monkeypatch) -> dict[str, str]:  # type: ignore[no-untyped-def]
    delivered: dict[str, str] = {}

    def capture(*, recipient: str, token: str) -> None:
        delivered.update(recipient=recipient, token=token)

    monkeypatch.setattr("app.api.routes.admin.deliver_membership_invitation", capture)
    return delivered


def test_invitation_is_committed_before_delivery_and_failed_token_is_revoked(
    client: TestClient, auth_headers, monkeypatch, owner_session
) -> None:  # type: ignore[no-untyped-def]
    recipient = "delivery-failure@nightingale.synthetic"

    def fail_after_observing_commit(*, recipient: str, token: str) -> None:
        invitation = owner_session.exec(
            select(ClinicInvitation).where(
                ClinicInvitation.clinic_id == demo_id("clinic-primary"),
                ClinicInvitation.email == recipient,
            )
        ).first()
        assert invitation is not None
        assert invitation.revoked_at is None
        assert invitation.token_hash
        assert token.startswith(f"{demo_id('clinic-primary')}.")
        raise RuntimeError("synthetic delivery failure")

    monkeypatch.setattr(
        "app.api.routes.admin.deliver_membership_invitation",
        fail_after_observing_commit,
    )
    response = client.post(
        "/api/v1/admin/memberships",
        headers=auth_headers("admin"),
        json={"email": recipient, "role": "staff"},
    )
    assert response.status_code == 503, response.text
    owner_session.expire_all()
    failed = owner_session.exec(
        select(ClinicInvitation).where(
            ClinicInvitation.clinic_id == demo_id("clinic-primary"),
            ClinicInvitation.email == recipient,
        )
    ).one()
    assert failed.revoked_at is not None

    delivered = _capture_invitation(monkeypatch)
    retried = client.post(
        "/api/v1/admin/memberships",
        headers=auth_headers("admin"),
        json={"email": recipient, "role": "staff"},
    )
    assert retried.status_code == 201, retried.text
    assert delivered["recipient"] == recipient


def test_admin_invites_then_recipient_accepts_before_membership_exists(
    client: TestClient, auth_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    admin = auth_headers("admin")
    delivered = _capture_invitation(monkeypatch)
    listed = client.get("/api/v1/admin/memberships", headers=admin)
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 5
    assert all("hashed_password" not in item for item in listed.json()["data"])

    invited = client.post(
        "/api/v1/admin/memberships",
        headers=admin,
        json={
            "email": "invited-clinician@nightingale.synthetic",
            "full_name": "Invited Synthetic Clinician",
            "role": "clinician",
        },
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["role"] == "clinician"
    assert invited.json()["state"] == "pending"
    assert {
        "token",
        "token_hash",
        "temporary_password",
        "user_id",
        "is_active",
    }.isdisjoint(invited.json())
    assert delivered["recipient"] == "invited-clinician@nightingale.synthetic"

    wrong_email = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json={
            "email": "wrong-recipient@nightingale.synthetic",
            "token": delivered["token"],
            "password": "recipient-chosen-password",
        },
    )
    assert wrong_email.status_code == 400

    # The admin cannot set a password through an ignored legacy field.
    forbidden_password = client.post(
        "/api/v1/admin/memberships",
        headers=admin,
        json={
            "email": "ignored-password@nightingale.synthetic",
            "role": "staff",
            "temporary_password": "attacker-controlled-password",
        },
    )
    assert forbidden_password.status_code == 422

    duplicate = client.post(
        "/api/v1/admin/memberships",
        headers=admin,
        json={
            "email": "invited-clinician@nightingale.synthetic",
            "role": "staff",
        },
    )
    assert duplicate.status_code == 409
    assert client.get("/api/v1/admin/memberships", headers=admin).json()["count"] == 5

    accepted = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json={
            "email": "invited-clinician@nightingale.synthetic",
            "token": delivered["token"],
            "password": "recipient-chosen-password",
            "full_name": "Recipient Verified Name",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "clinician"
    assert accepted.json()["is_active"] is True
    assert client.get("/api/v1/admin/memberships", headers=admin).json()["count"] == 6

    replay = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json={
            "email": "invited-clinician@nightingale.synthetic",
            "token": delivered["token"],
            "password": "recipient-chosen-password",
        },
    )
    assert replay.status_code == 400

    deactivated = client.post(
        f"/api/v1/admin/memberships/{accepted.json()['id']}/deactivate",
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


def test_admin_invite_rejects_patient_self_and_existing_members(
    client: TestClient, auth_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _capture_invitation(monkeypatch)
    admin = auth_headers("admin")
    for email in (
        "admin@nightingale.synthetic",
        "staff@nightingale.synthetic",
    ):
        response = client.post(
            "/api/v1/admin/memberships",
            headers=admin,
            json={"email": email, "role": "staff"},
        )
        assert response.status_code == 409

    patient = client.post(
        "/api/v1/admin/memberships",
        headers=admin,
        json={"email": "patient-invite@example.com", "role": "patient"},
    )
    assert patient.status_code == 422


def test_deactivating_inviter_revokes_unaccepted_invitations(
    client: TestClient,
    auth_headers,
    monkeypatch,
    owner_session,
) -> None:  # type: ignore[no-untyped-def]
    second_user = User(
        email="inviter-admin@example.com",
        full_name="Invitation Admin",
        hashed_password=get_password_hash("inviter-admin-password"),
    )
    owner_session.add(second_user)
    owner_session.flush()
    second_membership = ClinicMembership(
        clinic_id=demo_id("clinic-primary"),
        user_id=second_user.id,
        role="admin",
    )
    owner_session.add(second_membership)
    owner_session.commit()
    second_token = create_access_token(
        second_user.id,
        timedelta(minutes=5),
        membership_id=second_membership.id,
        clinic_id=second_membership.clinic_id,
    )
    delivered = _capture_invitation(monkeypatch)
    invitation_response = client.post(
        "/api/v1/admin/memberships",
        headers={"Authorization": f"Bearer {second_token}"},
        json={"email": "pending-after-admin-removal@example.com", "role": "staff"},
    )
    assert invitation_response.status_code == 201, invitation_response.text

    deactivated = client.post(
        f"/api/v1/admin/memberships/{second_membership.id}/deactivate",
        headers=auth_headers("admin"),
    )
    assert deactivated.status_code == 200, deactivated.text
    owner_session.expire_all()
    invitation = owner_session.get(ClinicInvitation, invitation_response.json()["id"])
    assert invitation is not None and invitation.revoked_at is not None

    accepted = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json={
            "email": "pending-after-admin-removal@example.com",
            "token": delivered["token"],
            "password": "recipient-controlled-password",
        },
    )
    assert accepted.status_code == 400


def test_deactivated_recipient_cannot_reactivate_with_an_older_invitation(
    client: TestClient,
    auth_headers,
    monkeypatch,
    owner_session,
) -> None:  # type: ignore[no-untyped-def]
    email = "recipient-deactivated-after-invite@example.com"
    delivered = _capture_invitation(monkeypatch)
    invited = client.post(
        "/api/v1/admin/memberships",
        headers=auth_headers("admin"),
        json={"email": email, "role": "staff"},
    )
    assert invited.status_code == 201, invited.text

    user = User(
        email=email,
        full_name="Recipient Later Deactivated",
        hashed_password=get_password_hash("existing-recipient-password"),
    )
    owner_session.add(user)
    owner_session.flush()
    membership = ClinicMembership(
        clinic_id=demo_id("clinic-primary"), user_id=user.id, role="staff"
    )
    owner_session.add(membership)
    owner_session.commit()

    deactivated = client.post(
        f"/api/v1/admin/memberships/{membership.id}/deactivate",
        headers=auth_headers("admin"),
    )
    assert deactivated.status_code == 200, deactivated.text
    owner_session.expire_all()
    invitation = owner_session.get(ClinicInvitation, invited.json()["id"])
    assert invitation is not None and invitation.revoked_at is not None

    recovery = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json={
            "email": email,
            "token": delivered["token"],
            "password": "attempted-self-reactivation",
        },
    )
    assert recovery.status_code == 400


def test_cross_clinic_email_preoccupation_cannot_bind_victim_membership(
    client: TestClient,
    auth_headers,
    monkeypatch,
    owner_session,
) -> None:  # type: ignore[no-untyped-def]
    victim_email = "victim@example.com"
    attacker = User(
        email=victim_email,
        full_name="Attacker Chosen Name",
        hashed_password=get_password_hash("attacker-password-long"),
    )
    owner_session.add(attacker)
    owner_session.flush()
    owner_session.add(
        ClinicMembership(
            clinic_id=demo_id("clinic-other"),
            user_id=attacker.id,
            role="admin",
        )
    )
    owner_session.commit()

    delivered = _capture_invitation(monkeypatch)
    invited = client.post(
        "/api/v1/admin/memberships",
        headers=auth_headers("admin"),
        json={"email": victim_email, "full_name": None, "role": "clinician"},
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["full_name"] is None
    assert "Attacker Chosen Name" not in invited.text

    form: dict[str, Any] = {
        "username": victim_email,
        "password": "attacker-password-long",
    }
    before_accept = client.post(
        "/api/v1/auth/login",
        data=form,
        headers={"X-Clinic-ID": str(demo_id("clinic-primary"))},
    )
    assert before_accept.status_code == 403

    accepted = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json={
            "email": victim_email,
            "token": delivered["token"],
            "password": "victim-controlled-password",
            "full_name": "Verified Victim",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["user_id"] == str(attacker.id)

    old_password = client.post(
        "/api/v1/auth/login",
        data=form,
        headers={"X-Clinic-ID": str(demo_id("clinic-primary"))},
    )
    assert old_password.status_code == 400
    victim_login = client.post(
        "/api/v1/auth/login",
        data={"username": victim_email, "password": "victim-controlled-password"},
        headers={"X-Clinic-ID": str(demo_id("clinic-primary"))},
    )
    assert victim_login.status_code == 200


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


def test_concurrent_admin_deactivation_cannot_remove_every_admin(
    owner_session,
) -> None:  # type: ignore[no-untyped-def]
    primary_user = owner_session.get(User, demo_id("user-admin"))
    primary_membership = owner_session.get(
        ClinicMembership, demo_id("membership-admin")
    )
    assert primary_user is not None and primary_membership is not None
    second_user = User(
        email="second-admin@example.com",
        full_name="Second Admin",
        hashed_password=get_password_hash("second-admin-password"),
    )
    owner_session.add(second_user)
    owner_session.flush()
    second_membership = ClinicMembership(
        clinic_id=demo_id("clinic-primary"),
        user_id=second_user.id,
        role="admin",
    )
    owner_session.add(second_membership)
    owner_session.commit()

    def token(user: User, membership: ClinicMembership) -> str:
        return create_access_token(
            user.id,
            timedelta(minutes=5),
            membership_id=membership.id,
            clinic_id=membership.clinic_id,
        )

    primary_token = token(primary_user, primary_membership)
    second_token = token(second_user, second_membership)
    barrier = threading.Barrier(2)

    def deactivate(target_id: str, bearer: str) -> int:
        barrier.wait(timeout=5)
        with TestClient(app) as isolated:
            return isolated.post(
                f"/api/v1/admin/memberships/{target_id}/deactivate",
                headers={"Authorization": f"Bearer {bearer}"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deactivate, str(second_membership.id), primary_token)
        second = pool.submit(deactivate, str(primary_membership.id), second_token)
        statuses = [first.result(timeout=10), second.result(timeout=10)]

    assert statuses.count(200) == 1
    assert all(status in {200, 403, 409} for status in statuses)
    owner_session.expire_all()
    active_admins = owner_session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == demo_id("clinic-primary"),
            ClinicMembership.role == "admin",
            col(ClinicMembership.is_active).is_(True),
        )
    ).all()
    assert len(active_admins) == 1
