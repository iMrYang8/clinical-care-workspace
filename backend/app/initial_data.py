import logging

from sqlalchemy import create_engine
from sqlmodel import Session

from app.core.config import settings
from app.core.db import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init() -> None:
    migration_engine = create_engine(
        str(settings.MIGRATION_DATABASE_URL or settings.DATABASE_URL)
    )
    try:
        with Session(migration_engine) as session:
            init_db(session)
    finally:
        migration_engine.dispose()


def main() -> None:
    logger.info("Creating initial data")
    init()
    logger.info("Initial data created")


if __name__ == "__main__":
    main()
