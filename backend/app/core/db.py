from sqlmodel import Session, create_engine

from app.core.config import settings
from app.seed import seed_demo_data

engine = create_engine(str(settings.DATABASE_URL))


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
