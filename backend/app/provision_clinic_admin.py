"""Explicit, idempotent owner-only provisioning for a production clinic."""

from __future__ import annotations

import argparse
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlmodel import Session, select

from app.clinic_codes import normalize_clinic_code
from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.models import Clinic, ClinicMembership, User

logger = logging.getLogger(__name__)
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


@dataclass(frozen=True)
class ProvisionedClinic:
    clinic_id: uuid.UUID
    admin_user_id: uuid.UUID
    admin_membership_id: uuid.UUID
    worker_user_id: uuid.UUID
    worker_membership_id: uuid.UUID


def _require_matching_membership(
    session: Session, clinic: Clinic, user: User, role: str
) -> ClinicMembership:
    membership = session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == clinic.id,
            ClinicMembership.user_id == user.id,
        )
    ).first()
    if membership is None:
        membership = ClinicMembership(
            clinic_id=clinic.id,
            user_id=user.id,
            role=role,
        )
        session.add(membership)
        session.flush()
        return membership
    if membership.role != role or not membership.is_active:
        raise RuntimeError("PROVISION_MEMBERSHIP_CONFLICT")
    return membership


def provision_clinic_admin(
    session: Session,
    *,
    clinic_code: str,
    clinic_slug: str,
    clinic_name: str,
    admin_email: str,
    admin_password: str,
    worker_email: str,
) -> ProvisionedClinic:
    code = normalize_clinic_code(clinic_code)
    slug = clinic_slug.strip().lower()
    name = clinic_name.strip()
    normalized_admin_email = admin_email.strip().lower()
    normalized_worker_email = worker_email.strip().lower()
    if code is None or not _SLUG.fullmatch(slug) or not name:
        raise ValueError("PROVISION_INVALID_CLINIC")
    if not normalized_admin_email or normalized_admin_email == normalized_worker_email:
        raise ValueError("PROVISION_INVALID_IDENTITY")
    if not 16 <= len(admin_password) <= 200:
        raise ValueError("PROVISION_ADMIN_PASSWORD_INVALID_LENGTH")

    clinic = session.exec(select(Clinic).where(Clinic.slug == slug)).first()
    code_owner = session.exec(select(Clinic).where(Clinic.code == code)).first()
    if clinic is None:
        if code_owner is not None:
            raise RuntimeError("PROVISION_CLINIC_CODE_CONFLICT")
        clinic = Clinic(code=code, slug=slug, name=name)
        session.add(clinic)
        session.flush()
    elif (
        clinic.name != name
        or clinic.code != code
        or code_owner is None
        or code_owner.id != clinic.id
    ):
        raise RuntimeError("PROVISION_CLINIC_CONFLICT")

    admin = session.exec(
        select(User).where(User.email == normalized_admin_email)
    ).first()
    if admin is None:
        admin = User(
            email=normalized_admin_email,
            full_name="Clinic Administrator",
            hashed_password=get_password_hash(admin_password),
        )
        session.add(admin)
        session.flush()
    else:
        password_matches = False
        if admin.account_kind == "staff" and admin.hashed_password is not None:
            password_matches, _ = verify_password(admin_password, admin.hashed_password)
        if not password_matches or not admin.is_active:
            raise RuntimeError("PROVISION_ADMIN_IDENTITY_CONFLICT")
    admin_membership = _require_matching_membership(session, clinic, admin, "admin")

    worker = session.exec(
        select(User).where(User.email == normalized_worker_email)
    ).first()
    if worker is None:
        worker = User(
            email=normalized_worker_email,
            full_name=f"Nightingale Worker ({slug})",
            # Worker identity is server-owned and never password-authenticated.
            hashed_password=get_password_hash(secrets.token_urlsafe(48)),
            account_kind="service",
        )
        session.add(worker)
        session.flush()
    elif not worker.is_active or worker.account_kind != "service":
        raise RuntimeError("PROVISION_WORKER_IDENTITY_CONFLICT")
    worker_membership = _require_matching_membership(session, clinic, worker, "worker")
    session.commit()
    return ProvisionedClinic(
        clinic_id=clinic.id,
        admin_user_id=admin.id,
        admin_membership_id=admin_membership.id,
        worker_user_id=worker.id,
        worker_membership_id=worker_membership.id,
    )


def _value(argument: str | None, env_name: str) -> str:
    value = argument or os.getenv(env_name)
    if not value:
        raise RuntimeError(f"{env_name} is required")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(prog="provision-clinic-admin")
    parser.add_argument("--clinic-code")
    parser.add_argument("--clinic-slug")
    parser.add_argument("--clinic-name")
    parser.add_argument("--admin-email")
    parser.add_argument("--worker-email")
    args = parser.parse_args()
    if settings.MIGRATION_DATABASE_URL is None:
        raise RuntimeError("MIGRATION_DATABASE_URL is required for provisioning")
    with Session(create_engine(str(settings.MIGRATION_DATABASE_URL))) as session:
        result = provision_clinic_admin(
            session,
            clinic_code=_value(args.clinic_code, "NIGHTINGALE_PROVISION_CLINIC_CODE"),
            clinic_slug=_value(args.clinic_slug, "NIGHTINGALE_PROVISION_CLINIC_SLUG"),
            clinic_name=_value(args.clinic_name, "NIGHTINGALE_PROVISION_CLINIC_NAME"),
            admin_email=_value(args.admin_email, "NIGHTINGALE_PROVISION_ADMIN_EMAIL"),
            admin_password=_value(None, "NIGHTINGALE_PROVISION_ADMIN_PASSWORD"),
            worker_email=_value(
                args.worker_email, "NIGHTINGALE_PROVISION_WORKER_EMAIL"
            ),
        )
    logger.info(
        "Provisioned clinic_id=%s admin_membership_id=%s worker_membership_id=%s",
        result.clinic_id,
        result.admin_membership_id,
        result.worker_membership_id,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
