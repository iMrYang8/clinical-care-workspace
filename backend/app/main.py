from pathlib import Path

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings
from app.services.nightingale import VersionConflictError

FRONTEND_DIR = Path(__file__).parent / "frontend"


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.FASTAPI_ENV != "development":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_HOST],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)


@app.exception_handler(VersionConflictError)
async def version_conflict_handler(
    _request: Request, exc: VersionConflictError
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "code": "VERSION_CONFLICT",
            "message": "The entry changed since the supplied If-Match version",
            "current_version_id": str(exc.current_version_id),
        },
    )


app.include_router(api_router, prefix=settings.API_V1_STR)
if FRONTEND_DIR.exists():
    app.frontend("/", directory=FRONTEND_DIR)
