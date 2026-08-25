import uuid

from sqlalchemy import Connection, event, text
from sqlalchemy.orm import SessionTransaction
from sqlmodel import Session, create_engine

from app.core.config import settings
from app.seed import seed_demo_data

engine = create_engine(str(settings.DATABASE_URL))
RUNTIME_DATABASE_ROLE = "nightingale_app"

_RLS_CLINIC_SESSION_KEY = "nightingale.rls_clinic_id"


@event.listens_for(Session, "after_begin")
def _restore_rls_clinic_after_commit(
    session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    """Reapply the transaction-local tenant GUC on a session's next transaction."""

    clinic_id = session.info.get(_RLS_CLINIC_SESSION_KEY)
    if connection.dialect.name == "postgresql" and isinstance(clinic_id, str):
        connection.execute(
            text("SELECT set_config('app.current_clinic_id', :clinic_id, true)"),
            {"clinic_id": clinic_id},
        )


def set_rls_clinic(session: Session, clinic_id: uuid.UUID) -> None:
    """Bind a trusted clinic to this Session without leaking it through the pool."""

    session.info[_RLS_CLINIC_SESSION_KEY] = str(clinic_id)
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT set_config('app.current_clinic_id', :clinic_id, true)"),
            {"clinic_id": str(clinic_id)},
        )


def assert_restricted_runtime_connection(connection: Connection) -> None:
    """Fail closed if a long-running process can bypass tenant RLS."""

    if connection.dialect.name != "postgresql":
        raise RuntimeError("UNSAFE_DATABASE_RUNTIME_ROLE")
    role = connection.execute(
        text(
            """
            SELECT current_user AS role_name,
                   r.rolcanlogin,
                   r.rolsuper,
                   r.rolcreatedb,
                   r.rolcreaterole,
                   r.rolreplication,
                   r.rolbypassrls,
                   EXISTS (
                     SELECT 1
                     FROM pg_class AS c
                     JOIN pg_namespace AS n ON n.oid = c.relnamespace
                     WHERE n.nspname = current_schema()
                       AND c.relrowsecurity
                       AND c.relowner = r.oid
                   ) AS owns_rls_table,
                   EXISTS (
                     SELECT 1
                     FROM pg_auth_members AS membership
                     WHERE membership.member = r.oid
                       AND membership.set_option
                   ) AS has_settable_membership
            FROM pg_roles AS r
            WHERE r.rolname = current_user
            """
        )
    ).one_or_none()
    if (
        role is None
        or role.role_name != RUNTIME_DATABASE_ROLE
        or not role.rolcanlogin
        or role.rolsuper
        or role.rolcreatedb
        or role.rolcreaterole
        or role.rolreplication
        or role.rolbypassrls
        or role.owns_rls_table
        or role.has_settable_membership
    ):
        raise RuntimeError("UNSAFE_DATABASE_RUNTIME_ROLE")


def assert_restricted_runtime_database() -> None:
    with engine.connect() as connection:
        assert_restricted_runtime_connection(connection)


# make sure all SQLModel models are imported (app.models) before initializing DB
# otherwise, SQLModel might fail to initialize relationships properly
# for more details: https://github.com/fastapi/full-stack-fastapi-template/issues/28


def init_db(session: Session) -> None:
    # Tables should be created with Alembic migrations
    # But if you don't want to use migrations, create
    # the tables un-commenting the next lines
    # from sqlmodel import SQLModel

    # This works because the models are already imported and registered from app.models
    # SQLModel.metadata.create_all(engine)

    if settings.FASTAPI_ENV == "development" and settings.ENABLE_DEMO_AUTH:
        seed_demo_data(session)
