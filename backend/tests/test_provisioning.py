import logging

import pytest
from sqlmodel import Session, col, func, select

from app.models import Clinic, ClinicMembership, User
from app.provision_clinic_admin import provision_clinic_admin


def test_explicit_production_provisioning_is_idempotent(
    owner_session: Session,
) -> None:
    arguments = {
        "clinic_slug": "production-fixture",
        "clinic_name": "Production Fixture Clinic",
        "admin_email": "owner@production-fixture.test",
        "admin_password": "synthetic-owner-password-123",
        "worker_email": "worker@production-fixture.test",
    }
    first = provision_clinic_admin(owner_session, **arguments)
    second = provision_clinic_admin(owner_session, **arguments)

    assert second == first
    assert (
        owner_session.exec(
            select(func.count())
            .select_from(Clinic)
            .where(Clinic.slug == arguments["clinic_slug"])
        ).one()
        == 1
    )
    users = owner_session.exec(
        select(User).where(
            col(User.email).in_([arguments["admin_email"], arguments["worker_email"]])
        )
    ).all()
    memberships = owner_session.exec(
        select(ClinicMembership).where(ClinicMembership.clinic_id == first.clinic_id)
    ).all()
    assert len(users) == 2
    assert {(item.role, item.is_active) for item in memberships} == {
        ("admin", True),
        ("worker", True),
    }


def test_provisioning_rejects_conflicts_and_never_logs_password(
    owner_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = "synthetic-secret-not-for-logs-123"
    arguments = {
        "clinic_slug": "provision-conflict",
        "clinic_name": "Provision Conflict Clinic",
        "admin_email": "owner@provision-conflict.test",
        "admin_password": password,
        "worker_email": "worker@provision-conflict.test",
    }
    with caplog.at_level(logging.INFO):
        provision_clinic_admin(owner_session, **arguments)
    assert password not in caplog.text

    with pytest.raises(RuntimeError, match="PROVISION_CLINIC_CONFLICT"):
        provision_clinic_admin(
            owner_session,
            **{**arguments, "clinic_name": "Different Clinic"},
        )
    owner_session.rollback()
    with pytest.raises(RuntimeError, match="PROVISION_ADMIN_IDENTITY_CONFLICT"):
        provision_clinic_admin(
            owner_session,
            **{**arguments, "admin_password": "different-password-value-123"},
        )
    owner_session.rollback()
    assert password not in caplog.text
