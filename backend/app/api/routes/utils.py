from fastapi import APIRouter

from app.core.config import settings
from app.core.db import assert_restricted_runtime_database

router = APIRouter(prefix="/utils", tags=["utils"])


@router.get("/health-check/")
async def health_check() -> bool:
    if settings.FASTAPI_ENV != "development":
        assert_restricted_runtime_database()
    return True
