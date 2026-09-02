"""Owner-only, environment-driven platform administrator provisioning."""

from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.models import PlatformAdministrator, User

logger = logging.getLogger(__name__)


def provision_platform_administrator(
    session: Session, *, email: str, password: str, full_name: str
) -> PlatformAdministrator:
    normalized_email = email.strip().lower()
    if not normalized_email or not 16 <= len(password) <= 200:
        raise ValueError("PLATFORM_ADMIN_IDENTITY_INVALID")
    user = session.exec(select(User).where(User.email == normalized_email)).first()
    if user is None:
        user = User(
            email=normalized_email,
            full_name=full_name.strip() or "Platform Administrator",
            hashed_password=get_password_hash(password),
        )
        session.add(user)
        session.flush()
    else:
        matches = False
        if user.account_kind == "staff" and user.hashed_password is not None:
            matches, _ = verify_password(password, user.hashed_password)
        if not matches or not user.is_active:
            raise RuntimeError("PLATFORM_ADMIN_IDENTITY_CONFLICT")
    administrator = session.exec(
        select(PlatformAdministrator).where(PlatformAdministrator.user_id == user.id)
    ).first()
    if administrator is None:
        administrator = PlatformAdministrator(user_id=user.id)
        session.add(administrator)
    elif not administrator.is_active:
        raise RuntimeError("PLATFORM_ADMIN_INACTIVE")
    session.commit()
    session.refresh(administrator)
    return administrator


def main() -> None:
    if settings.MIGRATION_DATABASE_URL is None:
        raise RuntimeError("MIGRATION_DATABASE_URL is required")
    email = os.environ["NIGHTINGALE_PLATFORM_ADMIN_EMAIL"]
    password = os.environ["NIGHTINGALE_PLATFORM_ADMIN_PASSWORD"]
    full_name = os.getenv("NIGHTINGALE_PLATFORM_ADMIN_NAME", "Platform Administrator")
    with Session(create_engine(str(settings.MIGRATION_DATABASE_URL))) as session:
        administrator = provision_platform_administrator(
            session, email=email, password=password, full_name=full_name
        )
    logger.info("Provisioned platform administrator %s", administrator.id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
