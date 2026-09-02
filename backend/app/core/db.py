import uuid

from sqlalchemy import Connection, event, text
from sqlalchemy.orm import SessionTransaction
from sqlmodel import Session, create_engine

from app.core.config import settings
from app.seed import seed_demo_data

engine = create_engine(str(settings.DATABASE_URL))
RUNTIME_DATABASE_ROLE = "nightingale_app"

_RLS_CLINIC_SESSION_KEY = "nightingale.rls_clinic_id"
_RLS_ACTOR_SESSION_KEY = "nightingale.rls_actor_id"
_RLS_ROLE_SESSION_KEY = "nightingale.rls_actor_role"
_RLS_PATIENT_SESSION_KEY = "nightingale.rls_patient_id"
_RLS_INVITATION_TOKEN_SESSION_KEY = "nightingale.rls_invitation_token_hash"

_SESSION_GUCS = {
    _RLS_CLINIC_SESSION_KEY: "app.current_clinic_id",
    _RLS_ACTOR_SESSION_KEY: "app.current_actor_id",
    _RLS_ROLE_SESSION_KEY: "app.current_actor_role",
    _RLS_PATIENT_SESSION_KEY: "app.current_patient_id",
    _RLS_INVITATION_TOKEN_SESSION_KEY: "app.current_invitation_token_hash",
}


@event.listens_for(Session, "after_begin")
def _restore_rls_clinic_after_commit(
    session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    """Reapply the transaction-local tenant GUC on a session's next transaction."""

    if connection.dialect.name == "postgresql":
        for session_key, guc in _SESSION_GUCS.items():
            value = session.info.get(session_key)
            if isinstance(value, str):
                connection.execute(
                    text("SELECT set_config(:guc, :value, true)"),
                    {"guc": guc, "value": value},
                )


def _set_rls_value(session: Session, session_key: str, value: str) -> None:
    session.info[session_key] = value
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT set_config(:guc, :value, true)"),
            {"guc": _SESSION_GUCS[session_key], "value": value},
        )


def _clear_rls_value(session: Session, session_key: str) -> None:
    # Retain an explicit empty value so ``after_begin`` re-applies the clear on
    # every later transaction owned by this Session. Dropping the key would let
    # a connection-level fixture/default (or a prior transaction context) leak
    # back in after commit.
    session.info[session_key] = ""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT set_config(:guc, '', true)"),
            {"guc": _SESSION_GUCS[session_key]},
        )


def set_rls_clinic(session: Session, clinic_id: uuid.UUID) -> None:
    """Bind a trusted clinic to this Session without leaking it through the pool."""

    _set_rls_value(session, _RLS_CLINIC_SESSION_KEY, str(clinic_id))


def set_rls_actor(
    session: Session,
    actor_id: uuid.UUID,
    *,
    role: str | None = None,
    patient_id: uuid.UUID | None = None,
) -> None:
    """Bind the authenticated actor used by identity and patient projections."""

    _set_rls_value(session, _RLS_ACTOR_SESSION_KEY, str(actor_id))
    if role is not None:
        _set_rls_value(session, _RLS_ROLE_SESSION_KEY, role)
    if patient_id is not None:
        _set_rls_value(session, _RLS_PATIENT_SESSION_KEY, str(patient_id))


def set_rls_patient_bootstrap(session: Session, patient_id: uuid.UUID) -> None:
    """Bind an opaque-secret bootstrap to one patient without inventing an actor."""

    _clear_rls_value(session, _RLS_ACTOR_SESSION_KEY)
    _set_rls_value(session, _RLS_ROLE_SESSION_KEY, "patient")
    _set_rls_value(session, _RLS_PATIENT_SESSION_KEY, str(patient_id))


def set_rls_invitation_token_hash(session: Session, token_hash: str) -> None:
    """Bind the already-verified one-time staff invitation secret."""

    if len(token_hash) != 64 or any(
        character not in "0123456789abcdef" for character in token_hash
    ):
        raise ValueError("INVALID_INVITATION_TOKEN_HASH")
    _set_rls_value(session, _RLS_INVITATION_TOKEN_SESSION_KEY, token_hash)


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
