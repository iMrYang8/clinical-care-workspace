"""Create and harden the database login used by the long-running API.

This module is invoked only by ``scripts/prestart.sh`` while connected with the
migration/owner credential.  The API container never receives that credential.
"""

import logging

from sqlalchemy import create_engine, text

from app.core.config import settings

RUNTIME_DATABASE_ROLE = "nightingale_app"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def configure_runtime_role() -> None:
    password = settings.POSTGRES_APP_PASSWORD
    if not password:
        raise RuntimeError("POSTGRES_APP_PASSWORD is required during DB bootstrap")

    database_url = settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL
    migration_engine = create_engine(str(database_url))
    try:
        with migration_engine.begin() as connection:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("Nightingale runtime role requires PostgreSQL")

            # PostgreSQL utility statements do not accept a password bind directly.
            # format(..., %L) is evaluated by PostgreSQL and safely quotes the value;
            # the generated statement is never logged.
            role_exists = connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                {"role": RUNTIME_DATABASE_ROLE},
            )
            action = "ALTER" if role_exists else "CREATE"
            ddl = connection.scalar(
                text(
                    f"""
                    SELECT format(
                      '{action} ROLE {RUNTIME_DATABASE_ROLE} WITH LOGIN PASSWORD %L '
                      'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION '
                      'NOBYPASSRLS',
                      CAST(:password AS text)
                    )
                    """
                ),
                {"password": password},
            )
            if not isinstance(ddl, str):
                raise RuntimeError("PostgreSQL did not produce runtime role DDL")
            connection.exec_driver_sql(ddl)
            # NOINHERIT does not prevent SET ROLE in PostgreSQL 16. Remove every
            # pre-existing membership so a reused login cannot switch into an
            # owner, superuser, or BYPASSRLS role after bootstrap.
            memberships = connection.execute(
                text(
                    """
                    SELECT parent.rolname
                    FROM pg_auth_members AS membership
                    JOIN pg_roles AS member ON member.oid = membership.member
                    JOIN pg_roles AS parent ON parent.oid = membership.roleid
                    WHERE member.rolname = :role
                    """
                ),
                {"role": RUNTIME_DATABASE_ROLE},
            ).scalars()
            for parent_role in memberships:
                revoke = connection.scalar(
                    text(
                        f"SELECT format('REVOKE %I FROM {RUNTIME_DATABASE_ROLE}', CAST(:parent AS text))"
                    ),
                    {"parent": parent_role},
                )
                if not isinstance(revoke, str):
                    raise RuntimeError("PostgreSQL did not produce role revoke DDL")
                connection.exec_driver_sql(revoke)
    finally:
        migration_engine.dispose()

    logger.info("Restricted PostgreSQL runtime role is configured")


def main() -> None:
    configure_runtime_role()


if __name__ == "__main__":
    main()
