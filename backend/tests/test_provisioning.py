import logging

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.clinic_codes import normalize_clinic_code
from app.core.security import verify_password
from app.models import Clinic, ClinicMembership, User
from app.provision_clinic_admin import provision_clinic_admin


def test_explicit_production_provisioning_is_idempotent(
    owner_session: Session,
) -> None:
    arguments = {
        "clinic_code": "prodclinic",
        "clinic_slug": "production-fixture",
        "clinic_name": "Production Fixture Clinic",
        "admin_email": " Owner@Production-Fixture.Test ",
        "admin_password": "exactly-sixteen!",
        "worker_email": " Worker@Production-Fixture.Test ",
    }
    first = provision_clinic_admin(owner_session, **arguments)
    second = provision_clinic_admin(owner_session, **arguments)

    assert second == first
    clinic = owner_session.get(Clinic, first.clinic_id)
    assert clinic is not None
    assert clinic.code == "PRODCLINIC"
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
            col(User.email).in_(
                [
                    "owner@production-fixture.test",
                    "worker@production-fixture.test",
                ]
            )
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
        "clinic_code": "CONFLICT",
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

    with pytest.raises(RuntimeError, match="PROVISION_CLINIC_CODE_CONFLICT"):
        provision_clinic_admin(
            owner_session,
            **{
                **arguments,
                "clinic_slug": "another-clinic",
                "clinic_name": "Another Clinic",
                "admin_email": "owner@another-clinic.test",
                "worker_email": "worker@another-clinic.test",
            },
        )
    owner_session.rollback()
    assert password not in caplog.text


@pytest.mark.parametrize(
    "invalid_code", ["AB", "ABCDEFGHIJKLM", "ABC1", " ABC ", "AB C", "诊所"]
)
def test_provisioning_rejects_invalid_clinic_codes(
    owner_session: Session, invalid_code: str
) -> None:
    with pytest.raises(ValueError, match="PROVISION_INVALID_CLINIC"):
        provision_clinic_admin(
            owner_session,
            clinic_code=invalid_code,
            clinic_slug="invalid-code",
            clinic_name="Invalid Code Clinic",
            admin_email="owner@invalid-code.test",
            admin_password="valid-password-16",
            worker_email="worker@invalid-code.test",
        )


@pytest.mark.parametrize("password", ["x" * 15, "x" * 201])
def test_provisioning_enforces_password_length_boundaries(
    owner_session: Session, password: str
) -> None:
    with pytest.raises(ValueError, match="PROVISION_ADMIN_PASSWORD_INVALID_LENGTH"):
        provision_clinic_admin(
            owner_session,
            clinic_code="PASSWORD",
            clinic_slug="password-boundary",
            clinic_name="Password Boundary Clinic",
            admin_email="owner@password-boundary.test",
            admin_password=password,
            worker_email="worker@password-boundary.test",
        )


def test_database_enforces_clinic_code_format_and_uniqueness(
    owner_session: Session,
) -> None:
    owner_session.add_all(
        [
            Clinic(code="ABC", slug="minimum-code", name="Minimum Code"),
            Clinic(
                code="ABCDEFGHIJKL",
                slug="maximum-code",
                name="Maximum Code",
            ),
        ]
    )
    owner_session.flush()

    owner_session.add(Clinic(code="AB1", slug="bad-db-code", name="Bad DB Code"))
    with pytest.raises(IntegrityError):
        owner_session.flush()
    owner_session.rollback()

    owner_session.add(
        Clinic(code="NIGHTINGALE", slug="duplicate-code", name="Duplicate Code")
    )
    with pytest.raises(IntegrityError):
        owner_session.flush()
    owner_session.rollback()


def test_clinic_code_normalization_accepts_only_three_to_twelve_letters() -> None:
    assert normalize_clinic_code("abc") == "ABC"
    assert normalize_clinic_code("abcdefghijkl") == "ABCDEFGHIJKL"
    for invalid in (
        None,
        "AB",
        "ABCDEFGHIJKLM",
        "ABC1",
        " ABC ",
        "AB C",
        "诊所",
    ):
        assert normalize_clinic_code(invalid) is None


def test_provisioning_and_login_preserve_a_200_character_password_with_spaces(
    owner_session: Session, client
) -> None:
    password = " " + "x" * 198 + " "
    result = provision_clinic_admin(
        owner_session,
        clinic_code="maxpass",
        clinic_slug="max-password",
        clinic_name="Maximum Password Clinic",
        admin_email=" Owner@Max-Password.Test ",
        admin_password=password,
        worker_email=" Worker@Max-Password.Test ",
    )
    admin = owner_session.get(User, result.admin_user_id)
    assert admin is not None
    assert str(admin.email) == "owner@max-password.test"
    matches, _ = verify_password(password, admin.hashed_password)
    trimmed_matches, _ = verify_password(password.strip(), admin.hashed_password)
    assert matches is True
    assert trimmed_matches is False

    login = client.post(
        "/api/v1/auth/login",
        headers={"X-Clinic-Code": "maxpass"},
        data={
            "username": " OWNER@MAX-PASSWORD.TEST ",
            "password": password,
        },
    )
    assert login.status_code == 200, login.text
    trimmed = client.post(
        "/api/v1/auth/login",
        headers={"X-Clinic-Code": "MAXPASS"},
        data={
            "username": "owner@max-password.test",
            "password": password.strip(),
        },
    )
    assert trimmed.status_code == 400
    assert trimmed.json()["detail"] == "Incorrect clinic code, email, or password"
