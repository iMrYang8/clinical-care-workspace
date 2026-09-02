from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select
from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.models import (
    Clinic,
    ClinicInvitation,
    ClinicMembership,
    ClinicOperationalSetting,
    Job,
    JobAttempt,
    NotificationAttempt,
    NotificationOutbox,
    Patient,
    PatientAccessCredential,
    PatientOTPChallenge,
    PatientUserLink,
    PatientVisit,
    PlatformAuditEvent,
    User,
    WorkerHeartbeat,
    get_datetime_utc,
)
from app.services.messaging import (
    canonical_receipt_timestamp,
    clear_deterministic_inbox,
    deterministic_inbox_messages,
    receipt_signature,
)
from app.services.worker_heartbeat import AI_WORKER_KIND, AI_WORKER_VERSION


@pytest.fixture(autouse=True)
def _reset_deterministic_delivery_inbox() -> Any:
    clear_deterministic_inbox()
    yield
    clear_deterministic_inbox()


def _identity(index: int) -> dict[str, str]:
    return {
        "display_name": f"Shared Phone Patient {index}",
        "date_of_birth": f"1990-01-{index:02d}",
        "medical_record_number": f"MRN-PHONE-{index:03d}",
        "identity_document_type": "nric_fin",
        "identity_document_number": f"S80000{index:03d}A",
    }


def _create_patient(
    client: TestClient, staff: dict[str, str], index: int
) -> dict[str, object]:
    response = client.post(
        "/api/v1/patients",
        headers=staff | {"Idempotency-Key": f"phone-patient-{index}"},
        json=_identity(index),
    )
    assert response.status_code == 201, response.text
    payload: dict[str, object] = response.json()
    return payload


def _otp_for_challenge(owner_session: Session, challenge_id: str) -> str:
    digest = hashlib.sha256(f"patient-otp:{challenge_id}".encode()).hexdigest()
    owner_session.expire_all()
    notification = owner_session.exec(
        select(NotificationOutbox).where(NotificationOutbox.idempotency_key == digest)
    ).one()
    attempt = owner_session.exec(
        select(NotificationAttempt)
        .where(NotificationAttempt.notification_id == notification.id)
        .order_by(col(NotificationAttempt.attempt_no).desc())
    ).first()
    assert attempt is not None and attempt.provider_message_id is not None
    message = next(
        item
        for item in deterministic_inbox_messages()
        if item.message_id == attempt.provider_message_id
    )
    otp = message.payload["otp"]
    assert isinstance(otp, str)
    assert notification.state == "submitted"
    return otp


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _without_observed_at(check: dict[str, Any]) -> dict[str, Any]:
    """Drop the live observation timestamp from a preflight check payload."""

    evidence = check.get("evidence")
    if not isinstance(evidence, dict):
        return dict(check)
    return dict(check) | {
        "evidence": {
            key: value for key, value in evidence.items() if key != "observed_at"
        }
    }


def test_shared_phone_enrollment_resend_revocation_and_recovery(
    client: TestClient, auth_headers: Any, owner_session: Session
) -> None:
    staff = auth_headers("staff")
    first_patient = _create_patient(client, staff, 1)
    second_patient = _create_patient(client, staff, 2)
    shared_phone = "+6591234567"

    provisions: list[dict[str, object]] = []
    for patient in (first_patient, second_patient):
        response = client.post(
            f"/api/v1/patients/{patient['id']}/patient-access",
            headers=staff,
            json={"phone": shared_phone, "channel": "whatsapp"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["notification_state"] == "submitted"
        sent = deterministic_inbox_messages(
            channel="whatsapp", destination=shared_phone
        )
        assert sent[-1].template_key == "patient-enrollment-v1"
        assert sent[-1].payload["portal_id"] == response.json()["access"]["portal_id"]
        provisions.append(response.json())

    wrong_claim = client.post(
        "/api/v1/patient-access/enroll/start",
        json={
            "invitation_token": provisions[0]["invitation_token"],
            "claim_code": "WRONG-CLAIM-CODE",
            "phone": shared_phone,
        },
    )
    assert wrong_claim.status_code == 400, wrong_claim.text
    assert wrong_claim.json()["detail"] == "Patient access is invalid"

    verified: list[dict[str, object]] = []
    for provision in provisions:
        start = client.post(
            "/api/v1/patient-access/enroll/start",
            json={
                "invitation_token": provision["invitation_token"],
                "claim_code": provision["claim_code"],
                "phone": shared_phone,
            },
        )
        assert start.status_code == 200, start.text
        challenge = start.json()
        assert challenge["delivery_state"] == "submitted"
        assert uuid.UUID(challenge["notification_id"])
        otp = _otp_for_challenge(owner_session, challenge["challenge_id"])
        verify = client.post(
            "/api/v1/patient-access/verify",
            json={"challenge_token": challenge["challenge_token"], "otp": otp},
        )
        assert verify.status_code == 200, verify.text
        verified.append(verify.json())

    first_me = client.get(
        "/api/v1/auth/me", headers=_bearer(verified[0]["token"]["access_token"])
    )
    second_me = client.get(
        "/api/v1/auth/me", headers=_bearer(verified[1]["token"]["access_token"])
    )
    assert first_me.status_code == second_me.status_code == 200
    assert first_me.json()["email"] is None
    assert second_me.json()["email"] is None
    assert first_me.json()["user_id"] != second_me.json()["user_id"]

    # Subsequent access needs only the non-identifying portal ID and a fresh
    # OTP; the shared number is never used as an account lookup key.
    first_access = provisions[0]["access"]
    assert isinstance(first_access, dict)
    first_portal_id = str(first_access["portal_id"])
    login_start = client.post(
        "/api/v1/patient-access/login/start",
        json={"portal_id": first_portal_id},
    )
    assert login_start.status_code == 200, login_start.text
    login_challenge = login_start.json()
    login_otp = _otp_for_challenge(owner_session, login_challenge["challenge_id"])
    wrong_otp = "000000" if login_otp != "000000" else "999999"
    for _attempt in range(5):
        rejected = client.post(
            "/api/v1/patient-access/verify",
            json={
                "challenge_token": login_challenge["challenge_token"],
                "otp": wrong_otp,
            },
        )
        assert rejected.status_code == 400, rejected.text
    exhausted = owner_session.get(
        PatientOTPChallenge, uuid.UUID(login_challenge["challenge_id"])
    )
    assert exhausted is not None
    owner_session.refresh(exhausted)
    assert exhausted.attempts_remaining == 0
    assert exhausted.revoked_at is not None
    still_rejected = client.post(
        "/api/v1/patient-access/verify",
        json={
            "challenge_token": login_challenge["challenge_token"],
            "otp": login_otp,
        },
    )
    assert still_rejected.status_code == 400, still_rejected.text

    login_start = client.post(
        "/api/v1/patient-access/login/start",
        json={"portal_id": first_portal_id},
    )
    assert login_start.status_code == 200, login_start.text
    login_challenge = login_start.json()
    login_otp = _otp_for_challenge(owner_session, login_challenge["challenge_id"])
    login_verify = client.post(
        "/api/v1/patient-access/verify",
        json={
            "challenge_token": login_challenge["challenge_token"],
            "otp": login_otp,
        },
    )
    assert login_verify.status_code == 200, login_verify.text
    login_me = client.get(
        "/api/v1/auth/me",
        headers=_bearer(login_verify.json()["token"]["access_token"]),
    )
    assert login_me.status_code == 200
    assert login_me.json()["user_id"] == first_me.json()["user_id"]

    owner_session.expire_all()
    credentials = owner_session.exec(
        select(PatientAccessCredential)
        .where(
            col(PatientAccessCredential.patient_id).in_(
                [first_patient["id"], second_patient["id"]]
            )
        )
        .order_by(col(PatientAccessCredential.created_at))
    ).all()
    assert len(credentials) == 2
    assert credentials[0].phone_hmac == credentials[1].phone_hmac
    assert credentials[0].phone_ciphertext != credentials[1].phone_ciphertext

    revoke = client.post(
        f"/api/v1/patients/{first_patient['id']}/patient-access/revoke",
        headers=staff,
        json={"reason_code": "phone_reassigned"},
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["access_state"] == "revoked"
    assert (
        client.get(
            "/api/v1/auth/me",
            headers=_bearer(verified[0]["token"]["access_token"]),
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/auth/me",
            headers=_bearer(verified[1]["token"]["access_token"]),
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/patient-access/login/start",
            json={"portal_id": first_portal_id},
        ).status_code
        == 400
    )
    owner_session.expire_all()
    revoked_enrollment = owner_session.get(
        NotificationOutbox, uuid.UUID(str(provisions[0]["notification_id"]))
    )
    assert revoked_enrollment is not None
    assert revoked_enrollment.state == "revoked"

    # Recovery remains available after a separate revocation request and can
    # safely assign a replacement number without reviving the old identity.
    replacement_phone = "+6598765432"
    recovery = client.post(
        f"/api/v1/patients/{first_patient['id']}/patient-access/recover",
        headers=staff,
        json={
            "phone": replacement_phone,
            "channel": "sms",
            "reason_code": "verified_phone_reassignment",
        },
    )
    assert recovery.status_code == 201, recovery.text
    recovered = recovery.json()
    start = client.post(
        "/api/v1/patient-access/enroll/start",
        json={
            "invitation_token": recovered["invitation_token"],
            "claim_code": recovered["claim_code"],
            "phone": replacement_phone,
        },
    )
    assert start.status_code == 200, start.text
    first_challenge = start.json()
    immediate_resend = client.post(
        "/api/v1/patient-access/resend",
        json={"challenge_token": first_challenge["challenge_token"]},
    )
    assert immediate_resend.status_code == 429

    challenge_row = owner_session.get(
        PatientOTPChallenge, uuid.UUID(first_challenge["challenge_id"])
    )
    assert challenge_row is not None
    challenge_row.resend_available_at = get_datetime_utc() - timedelta(seconds=1)
    owner_session.add(challenge_row)
    owner_session.commit()
    resent = client.post(
        "/api/v1/patient-access/resend",
        json={"challenge_token": first_challenge["challenge_token"]},
    )
    assert resent.status_code == 200, resent.text
    second_challenge = resent.json()
    assert second_challenge["challenge_id"] != first_challenge["challenge_id"]
    old_otp = _otp_for_challenge(owner_session, first_challenge["challenge_id"])
    assert (
        client.post(
            "/api/v1/patient-access/verify",
            json={
                "challenge_token": first_challenge["challenge_token"],
                "otp": old_otp,
            },
        ).status_code
        == 400
    )
    new_otp = _otp_for_challenge(owner_session, second_challenge["challenge_id"])
    recovered_verify = client.post(
        "/api/v1/patient-access/verify",
        json={
            "challenge_token": second_challenge["challenge_token"],
            "otp": new_otp,
        },
    )
    assert recovered_verify.status_code == 200, recovered_verify.text
    assert (
        recovered_verify.json()["access"]["portal_id"]
        != provisions[0]["access"]["portal_id"]
    )

    owner_session.expire_all()
    links = owner_session.exec(
        select(PatientUserLink).where(PatientUserLink.patient_id == first_patient["id"])
    ).all()
    assert len(links) == 1
    memberships = owner_session.exec(
        select(ClinicMembership).where(
            ClinicMembership.role == "patient",
            ClinicMembership.user_id.in_(
                [uuid.UUID(first_me.json()["user_id"]), links[0].user_id]
            ),
        )
    ).all()
    assert sorted(item.is_active for item in memberships) == [False, True]
    recovered_credential = owner_session.get(
        PatientAccessCredential,
        uuid.UUID(recovered_verify.json()["access"]["credential_id"]),
    )
    assert recovered_credential is not None
    assert recovered_credential.recovery_version == 2


def _onboarding_payload() -> dict[str, object]:
    return {
        "code": "CLINICB",
        "slug": "clinic-b",
        "name": "Nightingale Clinic B",
        "timezone": "Asia/Kuala_Lumpur",
        "initial_staff": [
            {
                "email": "clinic.b.admin@example.com",
                "full_name": "Clinic B Admin",
                "role": "admin",
            },
            {
                "email": "clinic.b.clinician@example.com",
                "full_name": "Clinic B Clinician",
                "role": "clinician",
            },
        ],
        "worker_enabled": True,
        "supported_languages": ["en", "ms", "nan", "zh"],
        "messaging_channels": ["email", "sms", "whatsapp"],
        "remote_text_egress_enabled": False,
        "remote_audio_egress_enabled": False,
        "calibration_required": True,
    }


def test_second_clinic_preflight_onboarding_is_data_only_audited_and_idempotent(
    client: TestClient,
    auth_headers: Any,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_a_headers = auth_headers("clinician")
    clinic_a_patients = client.get("/api/v1/patients", headers=clinic_a_headers)
    assert clinic_a_patients.status_code == 200, clinic_a_patients.text
    clinic_a_patient_id = clinic_a_patients.json()["data"][0]["id"]

    login = client.post(
        "/api/v1/platform/auth/login",
        json={
            "email": "platform.admin@nightingale.example",
            "password": "local-platform-owner-only",
        },
    )
    assert login.status_code == 200, login.text
    client.cookies.set(settings.PLATFORM_AUTH_COOKIE_NAME, login.json()["access_token"])
    client.headers.update({"Origin": str(settings.FRONTEND_HOST).rstrip("/")})
    invalid = _onboarding_payload() | {
        "timezone": "Mars/Olympus",
        "supported_languages": ["en"],
        "messaging_channels": ["sms"],
    }
    blocked = client.post("/api/v1/platform/clinics/preflight", json=invalid)
    assert blocked.status_code == 200, blocked.text
    failed_checks = {
        item["key"] for item in blocked.json()["checks"] if not item["passed"]
    }
    assert {"timezone", "languages", "messaging"}.issubset(failed_checks)

    with monkeypatch.context() as scoped:
        scoped.setattr(settings, "NOTIFICATION_SMS_PROVIDER", "disabled")
        capability_blocked = client.post(
            "/api/v1/platform/clinics/preflight", json=_onboarding_payload()
        )
        assert capability_blocked.status_code == 200
        messaging_check = next(
            item
            for item in capability_blocked.json()["checks"]
            if item["key"] == "messaging"
        )
        assert messaging_check["key"] == "messaging"
        assert messaging_check["passed"] is False
        assert messaging_check["reason_code"] == "messaging_capability_unavailable"
        messaging_evidence = messaging_check["evidence"]
        assert messaging_evidence["source"] == "deployment"
        assert messaging_evidence["evidence_id"].startswith("deployment:messaging:")
        assert [item["channel"] for item in messaging_evidence["channels"]] == [
            "email",
            "sms",
            "whatsapp",
        ]
        blocked_channel = next(
            item
            for item in messaging_evidence["channels"]
            if item["channel"] == "sms"
        )
        assert blocked_channel["provider"] == "disabled"
        assert blocked_channel["configured"] is False
        assert blocked_channel["production_safe"] is False
        assert blocked_channel["reason_code"] == "channel_disabled"
    with monkeypatch.context() as scoped:
        scoped.setattr(settings, "AI_WORKER_ENABLED", False)
        capability_blocked = client.post(
            "/api/v1/platform/clinics/preflight", json=_onboarding_payload()
        )
        worker_check = next(
            item
            for item in capability_blocked.json()["checks"]
            if item["key"] == "worker"
        )
        assert worker_check["passed"] is False
        assert worker_check["reason_code"] == "worker_capability_disabled"
    with monkeypatch.context() as scoped:
        scoped.setattr(
            settings,
            "EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE",
            "unqualified",
        )
        retention_blocked = client.post(
            "/api/v1/platform/clinics/preflight", json=_onboarding_payload()
        )
        retention_check = next(
            item
            for item in retention_blocked.json()["checks"]
            if item["key"] == "observability_retention"
        )
        assert retention_check["key"] == "observability_retention"
        assert retention_check["passed"] is False
        assert retention_check["reason_code"] == (
            "external_retention_evidence_unqualified"
        )
        retention_evidence = retention_check["evidence"]
        assert retention_evidence["source"] == "stored_policy"
        assert retention_evidence["retention_evidence"] == "unqualified"
        assert retention_evidence["proxy_retention_days"] == 30
        assert retention_evidence["container_retention_days"] == 30
        assert retention_evidence["apm_retention_days"] == 30
        blocked_onboarding = client.post(
            "/api/v1/platform/clinics/onboard",
            headers={"Idempotency-Key": "clinic-b-unqualified-retention"},
            json=_onboarding_payload(),
        )
        assert blocked_onboarding.status_code == 422, blocked_onboarding.text
        blocked_detail = blocked_onboarding.json()["detail"]
        assert blocked_detail["code"] == "CLINIC_PREFLIGHT_BLOCKED"
        # The onboarding route observes the same policy independently, so only
        # the observation timestamp may differ from the preflight response.
        assert [_without_observed_at(item) for item in blocked_detail["checks"]] == [
            _without_observed_at(retention_check)
        ]
    with monkeypatch.context() as scoped:
        scoped.setattr(settings, "EXTERNAL_APM_RETENTION_DAYS", 31)
        retention_blocked = client.post(
            "/api/v1/platform/clinics/preflight", json=_onboarding_payload()
        )
        retention_check = next(
            item
            for item in retention_blocked.json()["checks"]
            if item["key"] == "observability_retention"
        )
        assert retention_check["passed"] is False
        assert retention_check["reason_code"] == "external_retention_window_invalid"

    owner_session.expire_all()
    heartbeat = owner_session.exec(
        select(WorkerHeartbeat).where(WorkerHeartbeat.worker_kind == AI_WORKER_KIND)
    ).one()
    heartbeat.updated_at = get_datetime_utc() - timedelta(
        seconds=settings.AI_WORKER_HEARTBEAT_MAX_AGE_SECONDS + 1
    )
    owner_session.add(heartbeat)
    owner_session.commit()
    stale_worker = client.post(
        "/api/v1/platform/clinics/preflight", json=_onboarding_payload()
    )
    stale_check = next(
        item for item in stale_worker.json()["checks"] if item["key"] == "worker"
    )
    assert stale_check["reason_code"] == "worker_heartbeat_stale"

    owner_session.expire_all()
    heartbeat = owner_session.exec(
        select(WorkerHeartbeat).where(WorkerHeartbeat.worker_kind == AI_WORKER_KIND)
    ).one()
    heartbeat.worker_version = "incompatible-worker-version"
    heartbeat.updated_at = get_datetime_utc()
    owner_session.add(heartbeat)
    owner_session.commit()
    wrong_version = client.post(
        "/api/v1/platform/clinics/preflight", json=_onboarding_payload()
    )
    version_check = next(
        item for item in wrong_version.json()["checks"] if item["key"] == "worker"
    )
    assert version_check["reason_code"] == "worker_version_mismatch"
    owner_session.expire_all()
    heartbeat = owner_session.exec(
        select(WorkerHeartbeat).where(WorkerHeartbeat.worker_kind == AI_WORKER_KIND)
    ).one()
    heartbeat.worker_version = AI_WORKER_VERSION
    heartbeat.updated_at = get_datetime_utc()
    owner_session.add(heartbeat)
    owner_session.commit()

    payload = _onboarding_payload()
    preflight = client.post("/api/v1/platform/clinics/preflight", json=payload)
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["ready"] is True
    retention_check = next(
        item
        for item in preflight.json()["checks"]
        if item["key"] == "observability_retention"
    )
    assert retention_check["key"] == "observability_retention"
    assert retention_check["passed"] is True
    assert retention_check["reason_code"] is None
    qualified_evidence = retention_check["evidence"]
    assert qualified_evidence["source"] == "stored_policy"
    assert qualified_evidence["retention_evidence"] == (
        settings.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE
    )
    assert qualified_evidence["proxy_retention_days"] == (
        settings.EXTERNAL_PROXY_RETENTION_DAYS
    )
    assert qualified_evidence["container_retention_days"] == (
        settings.EXTERNAL_CONTAINER_RETENTION_DAYS
    )
    assert qualified_evidence["apm_retention_days"] == (
        settings.EXTERNAL_APM_RETENTION_DAYS
    )
    assert (
        preflight.json()["settings"]["external_proxy_retention_days"]
        == settings.EXTERNAL_PROXY_RETENTION_DAYS
    )
    assert (
        preflight.json()["settings"]["external_container_retention_days"]
        == settings.EXTERNAL_CONTAINER_RETENTION_DAYS
    )
    assert (
        preflight.json()["settings"]["external_apm_retention_days"]
        == settings.EXTERNAL_APM_RETENTION_DAYS
    )
    assert (
        preflight.json()["settings"]["external_observability_retention_evidence"]
        == settings.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE
    )
    created = client.post(
        "/api/v1/platform/clinics/onboard",
        headers={"Idempotency-Key": "clinic-b-onboarding-v1"},
        json=payload,
    )
    assert created.status_code == 201, created.text
    clinic_id = uuid.UUID(created.json()["id"])

    owner_session.expire_all()
    clinic = owner_session.get(Clinic, clinic_id)
    operational = owner_session.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == clinic_id
        )
    ).one()
    worker_membership = owner_session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == clinic_id,
            ClinicMembership.role == "worker",
            col(ClinicMembership.is_active).is_(True),
        )
    ).one()
    worker = owner_session.get(User, worker_membership.user_id)
    invitations = owner_session.exec(
        select(ClinicInvitation).where(ClinicInvitation.clinic_id == clinic_id)
    ).all()
    deliveries = owner_session.exec(
        select(NotificationOutbox).where(
            NotificationOutbox.clinic_id == clinic_id,
            NotificationOutbox.purpose == "staff_invitation",
        )
    ).all()
    assert clinic is not None and clinic.code == "CLINICB"
    assert operational.onboarding_status == "ready"
    assert operational.supported_languages_json == ["en", "ms", "nan", "zh"]
    assert operational.messaging_channels_json == ["email", "sms", "whatsapp"]
    assert (
        operational.external_proxy_retention_days
        == settings.EXTERNAL_PROXY_RETENTION_DAYS
    )
    assert (
        operational.external_container_retention_days
        == settings.EXTERNAL_CONTAINER_RETENTION_DAYS
    )
    assert (
        operational.external_apm_retention_days == settings.EXTERNAL_APM_RETENTION_DAYS
    )
    assert (
        operational.external_observability_retention_evidence
        == settings.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE
    )
    assert (
        operational.external_observability_retention_evidence_id
        == settings.EXTERNAL_OBSERVABILITY_RETENTION_EVIDENCE_ID
    )
    assert worker is not None and worker.account_kind == "service"
    assert len(invitations) == len(deliveries) == 2
    assert all(item.state == "submitted" for item in deliveries)

    admin_message = next(
        item
        for item in deterministic_inbox_messages(
            channel="email", destination="clinic.b.admin@example.com"
        )
        if item.template_key == "staff-invitation-v1"
    )
    staff_acceptance = client.post(
        "/api/v1/auth/invitations/accept",
        json={
            "token": admin_message.payload["invitation_token"],
            "email": "clinic.b.admin@example.com",
            "password": "clinic-b-admin-passphrase",
            "full_name": "Clinic B Admin",
        },
    )
    assert staff_acceptance.status_code == 200, staff_acceptance.text
    assert staff_acceptance.json()["role"] == "admin"

    # Complete the same journey through the clinical invitation rather than
    # treating row creation as proof that Clinic B is operational.  The token
    # is observed at the fake provider boundary, accepted once, and then the
    # newly provisioned staff identity must authenticate through the normal
    # clinic-scoped password surface.
    clinician_message = next(
        item
        for item in deterministic_inbox_messages(
            channel="email", destination="clinic.b.clinician@example.com"
        )
        if item.template_key == "staff-invitation-v1"
    )
    clinician_acceptance = client.post(
        "/api/v1/auth/invitations/accept",
        json={
            "token": clinician_message.payload["invitation_token"],
            "email": "clinic.b.clinician@example.com",
            "password": "clinic-b-clinician-passphrase",
            "full_name": "Clinic B Clinician",
        },
    )
    assert clinician_acceptance.status_code == 200, clinician_acceptance.text
    assert clinician_acceptance.json()["role"] == "clinician"
    clinic_b_login = client.post(
        "/api/v1/auth/login",
        headers={"X-Clinic-Code": "CLINICB"},
        data={
            "username": "clinic.b.clinician@example.com",
            "password": "clinic-b-clinician-passphrase",
        },
    )
    assert clinic_b_login.status_code == 200, clinic_b_login.text
    clinic_b_headers = _bearer(clinic_b_login.json()["access_token"])
    clinic_b_me = client.get("/api/v1/auth/me", headers=clinic_b_headers)
    assert clinic_b_me.status_code == 200, clinic_b_me.text
    assert clinic_b_me.json()["clinic_id"] == str(clinic_id)
    assert clinic_b_me.json()["clinic_code"] == "CLINICB"
    assert clinic_b_me.json()["role"] == "clinician"

    clinic_b_patient = client.post(
        "/api/v1/patients",
        headers=clinic_b_headers | {"Idempotency-Key": "clinic-b-patient-v1"},
        json={
            "display_name": "Clinic B Synthetic Patient",
            "date_of_birth": "1988-06-15",
            "medical_record_number": "CLINIC-B-MRN-001",
            "identity_document_type": "nric_fin",
            "identity_document_number": "S8800001B",
        },
    )
    assert clinic_b_patient.status_code == 201, clinic_b_patient.text
    clinic_b_patient_id = clinic_b_patient.json()["id"]

    source = client.post(
        "/api/v1/entries",
        headers=clinic_b_headers,
        json={
            "patient_id": clinic_b_patient_id,
            "section": "clinician",
            "title": "Clinic B synthetic consultation",
            "content": "The patient will return for a routine follow up next week.",
        },
    )
    assert source.status_code == 201, source.text
    clinic_b_job = client.post(
        f"/api/v1/patients/{clinic_b_patient_id}/ai/ingest",
        headers=clinic_b_headers | {"Idempotency-Key": "clinic-b-ai-job-v1"},
        json={
            "source_entry_version_id": source.json()["version_id"],
            "interaction_type": "doctor_consult",
        },
    )
    assert clinic_b_job.status_code == 200, clinic_b_job.text
    assert clinic_b_job.json()["patient_id"] == clinic_b_patient_id
    assert clinic_b_job.json()["attempt_count"] == 1
    assert clinic_b_job.json()["state"] in {"completed", "needs_review"}
    assert clinic_b_job.json()["ai_run"] is not None
    owner_session.expire_all()
    persisted_job = owner_session.get(Job, uuid.UUID(clinic_b_job.json()["id"]))
    persisted_attempt = owner_session.exec(
        select(JobAttempt).where(
            JobAttempt.clinic_id == clinic_id,
            JobAttempt.job_id == uuid.UUID(clinic_b_job.json()["id"]),
        )
    ).one()
    assert persisted_job is not None and persisted_job.clinic_id == clinic_id
    assert persisted_attempt.status == "completed"

    # Visits have no public write surface yet, so the synthetic fixture inserts
    # the schedule as the equivalent upstream scheduling-system contract.  The
    # notification itself still traverses the real Clinic-B API, outbox,
    # dispatcher, and observable provider boundary.
    visit = PatientVisit(
        clinic_id=clinic_id,
        patient_id=uuid.UUID(clinic_b_patient_id),
        visit_type="follow_up",
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
    )
    owner_session.add(visit)
    owner_session.commit()
    appointment_path = (
        f"/api/v1/patients/{clinic_b_patient_id}/visits/{visit.id}/notifications"
    )
    appointment = client.post(
        appointment_path,
        headers=clinic_b_headers | {"Idempotency-Key": "clinic-b-appointment-v1"},
        json={"channel": "whatsapp", "destination": "+60123456789"},
    )
    assert appointment.status_code == 201, appointment.text
    assert appointment.json()["state"] == "submitted"
    assert appointment.json()["attempt_count"] == 1
    appointment_message = next(
        item
        for item in deterministic_inbox_messages(
            channel="whatsapp", destination="+60123456789"
        )
        if item.template_key == "appointment-v1"
    )
    assert appointment_message.payload["visit_id"] == str(visit.id)

    # Exercise isolation from both directions using the newly authenticated
    # Clinic-B identity, rather than relying only on owner-side row inspection.
    clinic_b_patients = client.get("/api/v1/patients", headers=clinic_b_headers)
    assert clinic_b_patients.status_code == 200, clinic_b_patients.text
    assert {item["id"] for item in clinic_b_patients.json()["data"]} == {
        clinic_b_patient_id
    }
    assert (
        client.get(
            f"/api/v1/patients/{clinic_a_patient_id}", headers=clinic_b_headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/patients/{clinic_b_patient_id}", headers=clinic_a_headers
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/jobs/{clinic_b_job.json()['id']}", headers=clinic_a_headers
        ).status_code
        == 404
    )
    assert client.get(appointment_path, headers=clinic_a_headers).status_code == 404

    replay = client.post(
        "/api/v1/platform/clinics/onboard",
        headers={"Idempotency-Key": "clinic-b-onboarding-v1"},
        json=payload,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == created.json()["id"]
    owner_session.expire_all()
    assert len(
        owner_session.exec(
            select(ClinicInvitation).where(ClinicInvitation.clinic_id == clinic_id)
        ).all()
    ) == len(invitations)
    assert owner_session.exec(
        select(PlatformAuditEvent).where(
            PlatformAuditEvent.target_clinic_id == clinic_id,
            PlatformAuditEvent.action == "platform.clinic_onboarding_replayed",
        )
    ).first()

    conflict = client.post(
        "/api/v1/platform/clinics/onboard",
        headers={"Idempotency-Key": "clinic-b-onboarding-v1"},
        json=payload | {"timezone": "Asia/Singapore"},
    )
    assert conflict.status_code == 409


def _callback(
    client: TestClient,
    notification: NotificationOutbox,
    attempt: NotificationAttempt,
    *,
    event_id: str,
    event_type: str,
    signature_override: str | None = None,
    occurred_at: datetime | None = None,
) -> Any:
    occurred_at = occurred_at or datetime.now(UTC).replace(microsecond=0)
    body = {
        "notification_id": str(notification.id),
        "provider_event_id": event_id,
        "provider_message_id": attempt.provider_message_id,
        "event_type": event_type,
        "occurred_at": occurred_at.isoformat(),
    }
    canonical: dict[str, object] = {
        "notification_id": str(notification.id),
        "provider": attempt.provider,
        "provider_event_id": event_id,
        "provider_message_id": attempt.provider_message_id or "",
        "event_type": event_type,
        "occurred_at": canonical_receipt_timestamp(occurred_at),
    }
    return client.post(
        f"/api/v1/notification-webhooks/{notification.clinic_id}/{attempt.provider}",
        headers={
            "X-Notification-Signature": signature_override
            or receipt_signature(canonical),
            "Origin": str(settings.FRONTEND_HOST).rstrip("/"),
        },
        json=body,
    )


def test_appointment_delivery_callback_resend_revoke_and_idempotency(
    client: TestClient, auth_headers: Any, owner_session: Session
) -> None:
    staff = auth_headers("staff")
    patient_id = client.get("/api/v1/patients", headers=staff).json()["data"][0]["id"]
    patient = owner_session.get(Patient, uuid.UUID(patient_id))
    assert patient is not None
    visit = PatientVisit(
        clinic_id=patient.clinic_id,
        patient_id=patient.id,
        visit_type="follow_up",
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
    )
    owner_session.add(visit)
    owner_session.commit()
    path = f"/api/v1/patients/{visit.patient_id}/visits/{visit.id}/notifications"
    created = client.post(
        path,
        headers=staff | {"Idempotency-Key": "appointment-delivery-1"},
        json={"channel": "sms", "destination": "+6591112222"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["state"] == "submitted"
    assert created.json()["attempt_count"] == 1
    assert len(created.json()["attempts"]) == 1

    owner_session.expire_all()
    first = owner_session.get(NotificationOutbox, uuid.UUID(created.json()["id"]))
    assert first is not None
    first_attempt = owner_session.exec(
        select(NotificationAttempt).where(
            NotificationAttempt.notification_id == first.id,
            NotificationAttempt.attempt_no == 1,
        )
    ).one()
    bad_signature = _callback(
        client,
        first,
        first_attempt,
        event_id="bad-signature",
        event_type="delivered",
        signature_override="0" * 64,
    )
    assert bad_signature.status_code == 403
    failed = _callback(
        client,
        first,
        first_attempt,
        event_id="provider-failed-1",
        event_type="failed",
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    retry_delay = datetime.fromisoformat(
        failed.json()["available_at"]
    ) - datetime.fromisoformat(failed.json()["failed_at"])
    assert 25 <= retry_delay.total_seconds() <= 35
    failed_worklist = client.get(
        "/api/v1/notifications/worklist", params={"state": "failed"}, headers=staff
    )
    assert failed_worklist.status_code == 200, failed_worklist.text
    assert str(first.id) in {item["id"] for item in failed_worklist.json()}

    missing_destination = client.post(
        f"/api/v1/notifications/{first.id}/resend",
        headers=staff,
        json={"channel": "whatsapp"},
    )
    assert missing_destination.status_code == 422
    resent = client.post(
        f"/api/v1/notifications/{first.id}/resend",
        headers=staff,
        json={"channel": "whatsapp", "destination": "+6593334444"},
    )
    assert resent.status_code == 200, resent.text
    assert resent.json()["state"] == "submitted"
    assert resent.json()["attempt_count"] == 2
    assert len(resent.json()["attempts"]) == 2

    owner_session.expire_all()
    first = owner_session.get(NotificationOutbox, first.id)
    attempts = owner_session.exec(
        select(NotificationAttempt)
        .where(NotificationAttempt.notification_id == first.id)
        .order_by(col(NotificationAttempt.attempt_no))
    ).all()
    assert first is not None
    assert len(attempts) == 2
    assert attempts[0].provider_message_id != attempts[1].provider_message_id
    stale = _callback(
        client,
        first,
        attempts[0],
        event_id="late-delivery-old-attempt",
        event_type="delivered",
    )
    assert stale.status_code == 409, stale.text
    delivered_at = (
        datetime.now(UTC)
        .astimezone(timezone(timedelta(hours=8)))
        .replace(microsecond=0)
    )
    delivered = _callback(
        client,
        first,
        attempts[1],
        event_id="delivery-new-attempt",
        event_type="delivered",
        occurred_at=delivered_at,
    )
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["state"] == "delivered"
    acknowledged = client.post(
        f"/api/v1/notifications/{first.id}/acknowledge", headers=staff
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["state"] == "acknowledged"

    # An exact provider retry is idempotent and cannot regress a later local
    # acknowledgement. Reusing the provider event ID for different content is
    # rejected as a conflicting callback.
    duplicate = _callback(
        client,
        first,
        attempts[1],
        event_id="delivery-new-attempt",
        event_type="delivered",
        occurred_at=delivered_at,
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["state"] == "acknowledged"
    conflicting_event = _callback(
        client,
        first,
        attempts[1],
        event_id="delivery-new-attempt",
        event_type="failed",
        occurred_at=delivered_at,
    )
    assert conflicting_event.status_code == 409, conflicting_event.text

    conflict = client.post(
        path,
        headers=staff | {"Idempotency-Key": "appointment-delivery-1"},
        json={"channel": "email", "destination": "different@example.com"},
    )
    assert conflict.status_code == 409

    scheduled = client.post(
        path,
        headers=staff | {"Idempotency-Key": "appointment-delivery-scheduled"},
        json={
            "channel": "email",
            "destination": "patient@example.com",
            "scheduled_for": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert scheduled.status_code == 201, scheduled.text
    assert scheduled.json()["state"] == "queued"
    revoked = client.post(
        f"/api/v1/notifications/{scheduled.json()['id']}/revoke", headers=staff
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["state"] == "revoked"
    assert revoked.json()["attempt_count"] == 0

    naive_schedule = client.post(
        path,
        headers=staff,
        json={
            "channel": "sms",
            "destination": "+6591112222",
            "scheduled_for": "2030-01-01T10:00:00",
        },
    )
    assert naive_schedule.status_code == 422

    naive_callback = client.post(
        f"/api/v1/notification-webhooks/{first.clinic_id}/{attempts[1].provider}",
        headers={
            "X-Notification-Signature": "0" * 64,
            "Origin": str(settings.FRONTEND_HOST).rstrip("/"),
        },
        json={
            "notification_id": str(first.id),
            "provider_event_id": "naive-timestamp",
            "provider_message_id": attempts[1].provider_message_id,
            "event_type": "delivered",
            "occurred_at": "2030-01-01T10:00:00",
        },
    )
    assert naive_callback.status_code == 422

    stale_created = client.post(
        path,
        headers=staff | {"Idempotency-Key": "appointment-delivery-stale"},
        json={"channel": "sms", "destination": "+6594445555"},
    )
    assert stale_created.status_code == 201, stale_created.text
    stale_id = uuid.UUID(stale_created.json()["id"])
    owner_session.expire_all()
    stale_row = owner_session.get(NotificationOutbox, stale_id)
    assert stale_row is not None
    stale_row.submitted_at = get_datetime_utc() - timedelta(
        seconds=settings.NOTIFICATION_SUBMITTED_STALE_SECONDS + 1
    )
    owner_session.add(stale_row)
    owner_session.commit()
    attention = client.get(
        "/api/v1/notifications/worklist", params={"state": "attention"}, headers=staff
    )
    assert attention.status_code == 200, attention.text
    stale_public = next(
        item for item in attention.json() if item["id"] == str(stale_id)
    )
    assert stale_public["state"] == "failed"
    assert datetime.fromisoformat(stale_public["available_at"]) <= get_datetime_utc()


def test_twilio_status_callback_uses_exact_public_url_and_official_signature(
    client: TestClient,
    auth_headers: Any,
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staff = auth_headers("staff")
    patient_id = client.get("/api/v1/patients", headers=staff).json()["data"][0]["id"]
    patient = owner_session.get(Patient, uuid.UUID(patient_id))
    assert patient is not None
    visit = PatientVisit(
        clinic_id=patient.clinic_id,
        patient_id=patient.id,
        visit_type="follow_up",
        scheduled_at=get_datetime_utc() + timedelta(days=1),
    )
    owner_session.add(visit)
    owner_session.commit()
    provider_calls: list[dict[str, object]] = []
    message_sid = "SM" + ("1" * 32)

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        provider_calls.append({"url": url, **kwargs})
        return httpx.Response(
            201,
            request=httpx.Request("POST", url),
            json={"sid": message_sid},
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(settings, "NOTIFICATION_SMS_PROVIDER", "twilio")
        scoped.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_fixture")
        scoped.setattr(settings, "TWILIO_AUTH_TOKEN", "twilio-fixture-token")
        scoped.setattr(settings, "TWILIO_SMS_FROM", "+6590000000")
        scoped.setattr(settings, "NOTIFICATION_PUBLIC_BASE_URL", "http://testserver")
        scoped.setattr("app.services.messaging.httpx.post", fake_post)
        created = client.post(
            f"/api/v1/patients/{patient.id}/visits/{visit.id}/notifications",
            headers=staff | {"Idempotency-Key": "twilio-callback-contract"},
            json={"channel": "sms", "destination": "+6591112222"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["attempts"][0]["provider"] == "twilio-sms"
        assert len(provider_calls) == 1
        provider_data = provider_calls[0]["data"]
        assert isinstance(provider_data, dict)
        callback_url = provider_data["StatusCallback"]
        assert isinstance(callback_url, str)
        assert callback_url.endswith(f"/{patient.clinic_id}/{created.json()['id']}")

        form = {
            "MessageSid": message_sid,
            "MessageStatus": "delivered",
            "EventSid": "EZ" + ("2" * 32),
        }
        signature = RequestValidator("twilio-fixture-token").compute_signature(
            callback_url, form
        )
        callback_path = urlsplit(callback_url).path
        delivered = client.post(
            callback_path,
            data=form,
            headers={"X-Twilio-Signature": signature},
        )
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["state"] == "delivered"
        duplicate = client.post(
            callback_path,
            data=form,
            headers={"X-Twilio-Signature": signature},
        )
        assert duplicate.status_code == 200, duplicate.text
        assert len(duplicate.json()["receipts"]) == 1
        tampered = client.post(
            callback_path,
            data=form | {"MessageStatus": "undelivered"},
            headers={"X-Twilio-Signature": signature},
        )
        assert tampered.status_code == 403
