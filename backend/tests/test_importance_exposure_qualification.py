import uuid
from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, desc, select

from app.core.config import settings
from app.models import (
    ImportanceCandidateExposure,
    ImportanceCandidateSet,
    ImportanceExposureQualificationReport,
    ImportanceFeatureStat,
)


def _create_complete_surface_fixture(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
) -> tuple[str, str]:
    clinician = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=clinician).json()["data"][0][
        "id"
    ]
    protected_entry = client.post(
        "/api/v1/entries",
        headers=clinician,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Protected exposure fixture",
            "content": "prefix IMPORTANT suffix",
            "patient_facing": False,
        },
    ).json()
    protected = client.post(
        f"/api/v1/entries/{protected_entry['id']}/highlights",
        headers=clinician,
        json={
            "entry_version_id": protected_entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Protected review candidate",
            "critical": True,
            "unresolved": True,
            "feature_keys": ["risk:critical"],
        },
    )
    assert protected.status_code == 201, protected.text

    staff = auth_headers("staff")
    ordinary_entry = client.post(
        "/api/v1/entries",
        headers=staff,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Ordinary exposure fixture",
            "content": "prefix IMPORTANT suffix",
            "patient_facing": False,
        },
    ).json()
    ordinary = client.post(
        f"/api/v1/entries/{ordinary_entry['id']}/highlights",
        headers=staff,
        json={
            "entry_version_id": ordinary_entry["version_id"],
            "start_offset": 7,
            "end_offset": 16,
            "exact_quote": "IMPORTANT",
            "prefix": "prefix ",
            "suffix": " suffix",
            "label": "Ordinary priority candidate",
            "feature_keys": ["entity:diagnosis"],
        },
    )
    assert ordinary.status_code == 201, ordinary.text
    accepted = client.post(
        f"/api/v1/highlights/{ordinary.json()['id']}/accept", headers=staff
    )
    assert accepted.status_code == 200, accepted.text
    glance = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert glance.status_code == 200, glance.text
    assert ordinary.json()["id"] in {
        item["highlight_id"] for item in glance.json()["cards"]
    }
    assert protected.json()["id"] in {
        item["highlight_id"] for item in glance.json()["review_cards"]
    }
    return patient_id, ordinary.json()["id"]


def test_incomplete_exposure_set_fails_persisted_api_qualification(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
) -> None:
    _create_complete_surface_fixture(client, auth_headers)
    latest = owner_session.exec(
        select(ImportanceCandidateSet).order_by(
            desc(col(ImportanceCandidateSet.observed_at))
        )
    ).first()
    assert latest is not None
    missing = owner_session.exec(
        select(ImportanceCandidateExposure).where(
            ImportanceCandidateExposure.candidate_set_id == latest.candidate_set_id
        )
    ).first()
    assert missing is not None
    owner_session.delete(missing)
    owner_session.commit()

    response = client.post(
        "/api/v1/importance/exposure-reports",
        headers=auth_headers("clinician"),
        json={"window_hours": 24},
    )
    assert response.status_code == 201, response.text
    report = response.json()
    assert report["qualified"] is False
    assert report["current"] is False
    assert report["effective_mode"] == "shadow"
    assert report["candidate_count"] > report["telemetry_count"]
    assert report["missing_telemetry_count"] > 0
    assert "candidate_telemetry_missing" in report["qualification_reasons"]
    assert any(
        metrics["missing_telemetry_count"] > 0
        for metrics in report["surfaces"].values()
    )
    stored = owner_session.get(
        ImportanceExposureQualificationReport, uuid.UUID(report["id"])
    )
    assert stored is not None
    assert stored.qualified is False


def test_complete_report_qualifies_and_guards_active_mode(
    client: TestClient,
    auth_headers: Callable[[str], dict[str, str]],
    owner_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "IMPORTANCE_LEARNING_MODE", "active")
    patient_id, ordinary_highlight_id = _create_complete_surface_fixture(
        client, auth_headers
    )
    clinician = auth_headers("clinician")

    before = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert before.status_code == 200, before.text
    assert before.json()["importance_mode"] == "shadow"
    assert owner_session.exec(select(ImportanceFeatureStat)).first() is None

    created = client.post(
        "/api/v1/importance/exposure-reports",
        headers=clinician,
        json={"window_hours": 24},
    )
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["qualified"] is True
    assert report["current"] is True
    assert report["effective_mode"] == "active"
    assert report["missing_telemetry_count"] == 0
    assert report["duplicate_telemetry_count"] == 0
    assert report["protected_recall"] == 1.0
    assert report["ordinary_recall"] == 1.0
    assert report["ordinary_exposure_rate"] > 0
    assert set(report["surfaces"]) == {
        "current_priorities",
        "clinical_review",
    }

    current = client.get(
        "/api/v1/importance/exposure-reports/current", headers=clinician
    )
    assert current.status_code == 200, current.text
    assert current.json()["id"] == report["id"]
    active = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert active.status_code == 200, active.text
    assert active.json()["importance_mode"] == "active"

    learned = client.post(
        f"/api/v1/highlights/{ordinary_highlight_id}/pin",
        headers=auth_headers("staff") | {"Idempotency-Key": "qualified-active-pin"},
    )
    assert learned.status_code == 200, learned.text
    owner_session.expire_all()
    stat = owner_session.exec(
        select(ImportanceFeatureStat).where(
            ImportanceFeatureStat.feature_key == "entity:diagnosis"
        )
    ).one()
    assert stat.observation_count == 1
    assert stat.weight > 0

    stored_report = owner_session.get(
        ImportanceExposureQualificationReport, uuid.UUID(report["id"])
    )
    assert stored_report is not None
    # Qualification reports are immutable evidence and the database enforces
    # that their expiry follows the audited window. Advance the qualification
    # clock instead of corrupting that persisted evidence to exercise expiry.
    after_expiry = stored_report.expires_at + timedelta(seconds=1)
    monkeypatch.setattr(
        "app.services.importance.get_datetime_utc", lambda: after_expiry
    )
    demoted = client.get(f"/api/v1/patients/{patient_id}/glance", headers=clinician)
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["importance_mode"] == "shadow"

    assert (
        client.get(
            "/api/v1/importance/exposure-reports/current",
            headers=auth_headers("staff"),
        ).status_code
        == 403
    )
