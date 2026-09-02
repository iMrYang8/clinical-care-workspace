from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import engine, set_rls_actor, set_rls_clinic
from app.models import (
    AuditEvent,
    ClinicFormularyConcept,
    ClinicFormularyVersion,
)
from app.seed import demo_id
from app.services.clinical_formulary import seed_clinic_formulary_template


@pytest.mark.unit
def test_formulary_configuration_requires_all_languages_and_stable_digest() -> None:
    from app.models import ClinicFormularyConceptCreate
    from app.services.clinical_formulary import (
        FormularyConfigurationError,
        clinic_formulary_content_sha256,
    )

    valid = ClinicFormularyConceptCreate.model_validate(_concept())
    assert clinic_formulary_content_sha256([valid]) == (
        clinic_formulary_content_sha256([valid])
    )
    malformed = ClinicFormularyConceptCreate.model_validate(
        _concept()
        | {
            "multilingual_aliases": {
                "en": ["metformin"],
                "ms": ["metformin"],
                "zh": ["二甲双胍"],
            }
        }
    )
    with pytest.raises(
        FormularyConfigurationError,
        match="FORMULARY_MULTILINGUAL_ALIASES_INCOMPLETE",
    ):
        clinic_formulary_content_sha256([malformed])


def _concept(*, maximum_single_dose: float = 1_000) -> dict[str, object]:
    return {
        "concept_code": "rxnorm:860975",
        "canonical_name": "metformin",
        "multilingual_aliases": {
            "en": ["metformin"],
            "ms": ["metformin"],
            "nan": ["metformin"],
            "zh": ["二甲双胍", "二甲雙胍"],
        },
        "dose_unit": "mg",
        "minimum_single_dose": 250,
        "maximum_single_dose": maximum_single_dose,
        "permitted_routes": ["oral"],
        "contraindicated_allergy_concepts": [],
    }


def _version_payload(
    version_code: str,
    *,
    maximum_single_dose: float = 1_000,
) -> dict[str, Any]:
    return {
        "version_code": version_code,
        "concepts": [_concept(maximum_single_dose=maximum_single_dose)],
    }


def _create_qualify_activate(
    client: TestClient,
    headers: dict[str, str],
    version_code: str,
    *,
    maximum_single_dose: float = 1_000,
) -> dict[str, object]:
    created = client.post(
        "/api/v1/admin/formulary/versions",
        headers=headers,
        json=_version_payload(
            version_code,
            maximum_single_dose=maximum_single_dose,
        ),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["qualification_state"] == "unqualified"
    assert body["digest_matches"] is True
    assert body["content_locked_at"] is not None

    wrong_digest = client.post(
        f"/api/v1/admin/formulary/versions/{body['id']}/qualify",
        headers=headers,
        json={"expected_content_sha256": "0" * 64},
    )
    assert wrong_digest.status_code == 409, wrong_digest.text
    assert wrong_digest.json()["detail"]["code"] == (
        "FORMULARY_EXPECTED_DIGEST_MISMATCH"
    )

    qualified = client.post(
        f"/api/v1/admin/formulary/versions/{body['id']}/qualify",
        headers=headers,
        json={"expected_content_sha256": body["content_sha256"]},
    )
    assert qualified.status_code == 200, qualified.text
    assert qualified.json()["qualification_state"] == "qualified"
    assert qualified.json()["qualification_source"] == "clinic_admin"

    activated = client.post(
        f"/api/v1/admin/formulary/versions/{body['id']}/activate",
        headers=headers,
        json={"expected_content_sha256": body["content_sha256"]},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "active"
    assert activated.json()["qualification_state"] == "active"
    return cast(dict[str, Any], activated.json())


def test_admin_formulary_version_lifecycle_is_audited_fail_closed_and_single_active(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    admin = auth_headers("admin")
    staff = auth_headers("staff")
    forbidden = client.get("/api/v1/admin/formulary/versions", headers=staff)
    assert forbidden.status_code == 403

    malformed = _version_payload("clinic-v0")
    malformed_concept = _concept()
    malformed_concept["multilingual_aliases"] = {
        "en": ["metformin"],
        "ms": ["metformin"],
        "zh": ["二甲双胍"],
    }
    malformed["concepts"] = [malformed_concept]
    rejected = client.post(
        "/api/v1/admin/formulary/versions",
        headers=admin,
        json=malformed,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == (
        "FORMULARY_MULTILINGUAL_ALIASES_INCOMPLETE"
    )

    first = _create_qualify_activate(client, admin, "clinic-v1")
    readiness = client.get("/api/v1/admin/formulary/readiness", headers=admin)
    assert readiness.status_code == 200, readiness.text
    assert readiness.json() == {
        "ready": True,
        "reason_code": None,
        "active_version_id": first["id"],
        "version_code": "clinic-v1",
        "content_sha256": first["content_sha256"],
        "qualification_source": "clinic_admin",
    }

    second = _create_qualify_activate(
        client,
        admin,
        "clinic-v2",
        maximum_single_dose=1_500,
    )
    versions = client.get("/api/v1/admin/formulary/versions", headers=admin)
    assert versions.status_code == 200, versions.text
    assert versions.json()["count"] == 2
    statuses = {
        item["version_code"]: item["status"] for item in versions.json()["data"]
    }
    assert statuses == {"clinic-v1": "retired", "clinic-v2": "active"}
    assert second["id"] != first["id"]

    detail = client.get(
        f"/api/v1/admin/formulary/versions/{second['id']}", headers=admin
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["concept_count"] == 1
    assert set(detail.json()["concepts"][0]["multilingual_aliases"]) == {
        "en",
        "ms",
        "nan",
        "zh",
    }

    owner_session.expire_all()
    events = owner_session.exec(
        select(AuditEvent).where(
            AuditEvent.clinic_id == demo_id("clinic-primary"),
            AuditEvent.resource_id == uuid.UUID(str(second["id"])),
        )
    ).all()
    assert {event.action for event in events} >= {
        "clinic.formulary.version_created",
        "clinic.formulary.version_qualified",
        "clinic.formulary.version_activated",
    }
    assert all(event.clinical_rationale_ciphertext is None for event in events)


def test_formulary_content_is_database_immutable_and_rls_isolates_clinics(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    admin = auth_headers("admin")
    active = _create_qualify_activate(client, admin, "clinic-rls-v1")
    primary_id = demo_id("clinic-primary")
    other_id = demo_id("clinic-other")
    seed_clinic_formulary_template(
        owner_session,
        clinic_id=other_id,
        template="nightingale-clinic-formulary-v1",
    )
    owner_session.commit()

    with Session(engine) as runtime:
        set_rls_clinic(runtime, primary_id)
        set_rls_actor(runtime, demo_id("user-admin"), role="admin")
        visible = runtime.exec(select(ClinicFormularyVersion)).all()
        assert visible
        assert {row.clinic_id for row in visible} == {primary_id}

    owner_session.expire_all()
    concept = owner_session.exec(
        select(ClinicFormularyConcept).where(
            ClinicFormularyConcept.formulary_version_id == uuid.UUID(str(active["id"]))
        )
    ).one()
    with pytest.raises(DBAPIError, match="formulary concepts are immutable"):
        concept.canonical_name = "silently-corrected-name"
        owner_session.add(concept)
        owner_session.commit()
    owner_session.rollback()


def test_platform_preflight_reports_versioned_formulary_template_readiness(
    client: TestClient,
) -> None:
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
    preflight = client.post(
        "/api/v1/platform/clinics/preflight",
        json={
            "code": "FORMB",
            "slug": "formulary-clinic-b",
            "name": "Formulary Clinic B",
            "timezone": "Asia/Singapore",
            "initial_staff": [
                {
                    "email": "formulary.admin@example.com",
                    "full_name": "Formulary Admin",
                    "role": "admin",
                }
            ],
            "worker_enabled": True,
            "supported_languages": ["en", "ms", "nan", "zh"],
            "messaging_channels": ["email"],
            "remote_text_egress_enabled": False,
            "remote_audio_egress_enabled": False,
            "calibration_required": True,
            "formulary_template": "nightingale-clinic-formulary-v1",
        },
    )
    assert preflight.status_code == 200, preflight.text
    check = next(
        item for item in preflight.json()["checks"] if item["key"] == "formulary"
    )
    assert check["key"] == "formulary"
    assert check["passed"] is True
    assert check["reason_code"] is None
    assert check["evidence"]["source"] == "stored_policy"
    assert check["evidence"]["formulary_template"] == (
        "nightingale-clinic-formulary-v1"
    )
    assert check["evidence"]["evidence_id"] == (
        "stored-policy:formulary:nightingale-clinic-formulary-v1"
    )
    assert preflight.json()["settings"]["formulary_template"] == (
        "nightingale-clinic-formulary-v1"
    )
