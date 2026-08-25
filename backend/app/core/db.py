import uuid

from sqlalchemy import Connection, event, text
from sqlalchemy.orm import SessionTransaction
from sqlmodel import Session, create_engine

from app.core.config import settings
from app.seed import seed_demo_data

engine = create_engine(str(settings.DATABASE_URL))

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
