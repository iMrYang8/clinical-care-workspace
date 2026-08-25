from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sentry_sdk.types import Event, Hint
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings
from app.core.db import assert_restricted_runtime_database
from app.services.nightingale import VersionConflictError
from app.services.redaction import assert_presidio_runtime

FRONTEND_DIR = Path(__file__).parent / "frontend"


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


def _sanitize_sentry_event(event: Event, _hint: Hint) -> Event | None:
    """Remove clinical request bodies and credentials from error/trace events."""

    # Custom instrumentation can place identifiers or clinical values outside
    # Sentry's request object. Fail closed by dropping every free-form carrier;
    # exception types and core envelope metadata remain available for grouping.
    event_data = cast(dict[str, Any], event)
    for key in (
        "breadcrumbs",
        "contexts",
        "extra",
        "fingerprint",
        "logentry",
        "message",
        "spans",
        "stacktrace",
        "tags",
        "threads",
        "transaction",
        "user",
    ):
        event_data.pop(key, None)
    request = event.get("request")
    if isinstance(request, dict):
        for key in (
            "data",
            "body",
            "cookies",
            "headers",
            "env",
            "query_string",
            "url",
            "path_info",
            "fragment",
        ):
            request.pop(key, None)
    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    value["value"] = "REDACTED"
                    value.pop("stacktrace", None)
    return event


if settings.SENTRY_DSN and settings.FASTAPI_ENV != "development":
    sentry_sdk.init(
        dsn=str(settings.SENTRY_DSN),
        enable_tracing=False,
        traces_sample_rate=0.0,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=_sanitize_sentry_event,
        before_send_transaction=_sanitize_sentry_event,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if settings.FASTAPI_ENV != "development":
        assert_restricted_runtime_database()
        if settings.AI_PROVIDER == "openai" and settings.REMOTE_TEXT_EGRESS_ENABLED:
            assert_presidio_runtime(settings.PRESIDIO_NLP_MODEL)
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_HOST],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


class CookieCsrfMiddleware(BaseHTTPMiddleware):
    """Require a trusted browser Origin for cookie-authenticated mutations.

    Bearer callers remain available for explicit API and worker automation. The
    browser UI uses only the Secure SameSite cookie and therefore passes this
    independent origin check on every state-changing request.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        cookie_auth = bool(request.cookies.get(settings.AUTH_COOKIE_NAME))
        bearer_auth = (
            request.headers.get("authorization", "").lower().startswith("bearer ")
        )
        login_path = request.url.path in {
            f"{settings.API_V1_STR}/auth/login",
            f"{settings.API_V1_STR}/auth/demo-login",
        }
        if is_mutation and cookie_auth and not bearer_auth and not login_path:
            supplied = request.headers.get("origin")
            if supplied is None:
                referer = request.headers.get("referer")
                supplied = _origin(referer) if referer else None
            allowed = {_origin(settings.FRONTEND_HOST)}
            if supplied is None or _origin(supplied) not in allowed:
                return JSONResponse(
                    status_code=403, content={"detail": "CSRF origin rejected"}
                )
        return await call_next(request)


app.add_middleware(CookieCsrfMiddleware)


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
