import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from sentry_sdk.types import Event, Hint
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.main import api_router
from app.core.config import settings
from app.core.db import assert_restricted_runtime_database
from app.services.nightingale import VersionConflictError
from app.services.operational_events import (
    initialize_operational_event_store,
    purge_operational_events,
    record_operational_event,
    run_operational_event_purge_loop,
)
from app.services.redaction import assert_presidio_runtime

FRONTEND_DIR = Path(__file__).parent / "frontend"
access_logger = logging.getLogger("nightingale.access")
_SAFE_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_SAFE_EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_HTTP_METHODS = frozenset(
    {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
)


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0] if route.tags else 'frontend'}-{route.name}"


def _sanitize_sentry_event(event: Event, _hint: Hint) -> Event | None:
    """Return a minimal error envelope with no free-form request carriers."""

    # Build a new event instead of trying to enumerate every place custom
    # instrumentation might attach a URL, header, body, local, or exception
    # message. Only bounded SDK identifiers and code-defined exception types
    # survive; request data is absent by construction.
    source = cast(dict[str, Any], event)
    sanitized: dict[str, Any] = {}
    event_id = source.get("event_id")
    if isinstance(event_id, str) and _SAFE_EVENT_ID.fullmatch(event_id):
        sanitized["event_id"] = event_id
    level = source.get("level")
    if level in {"fatal", "error", "warning", "info", "debug"}:
        sanitized["level"] = level
    if source.get("platform") == "python":
        sanitized["platform"] = "python"

    exception = source.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            safe_values: list[dict[str, str]] = []
            for value in values:
                if isinstance(value, dict):
                    exception_type = value.get("type")
                    safe_type = (
                        exception_type
                        if isinstance(exception_type, str)
                        and _SAFE_EXCEPTION_TYPE.fullmatch(exception_type)
                        else "SanitizedError"
                    )
                    safe_values.append({"type": safe_type, "value": "REDACTED"})
            if safe_values:
                sanitized["exception"] = {"values": safe_values}
    return cast(Event, sanitized)


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
    initialize_operational_event_store()
    purge_operational_events()
    if settings.FASTAPI_ENV != "development":
        assert_restricted_runtime_database()
        if settings.AI_PROVIDER == "openai" and settings.REMOTE_TEXT_EGRESS_ENABLED:
            assert_presidio_runtime(settings.PRESIDIO_NLP_MODEL)
    purge_task = asyncio.create_task(
        run_operational_event_purge_loop(),
        name="operational-event-retention-purge",
    )
    try:
        yield
    finally:
        purge_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task


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


class CookieCsrfMiddleware:
    """Require a trusted browser Origin for cookie-authenticated mutations.

    Bearer callers remain available for explicit API and worker automation. The
    browser UI uses only the Secure SameSite cookie and therefore passes this
    independent origin check on every state-changing request.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        is_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        cookie_auth = bool(
            request.cookies.get(settings.AUTH_COOKIE_NAME)
            or request.cookies.get(settings.PLATFORM_AUTH_COOKIE_NAME)
        )
        bearer_auth = (
            request.headers.get("authorization", "").lower().startswith("bearer ")
        )
        login_path = request.url.path in {
            f"{settings.API_V1_STR}/auth/login",
            f"{settings.API_V1_STR}/auth/demo-login",
            f"{settings.API_V1_STR}/platform/auth/login",
            f"{settings.API_V1_STR}/patient-access/enroll/start",
            f"{settings.API_V1_STR}/patient-access/login/start",
            f"{settings.API_V1_STR}/patient-access/resend",
            f"{settings.API_V1_STR}/patient-access/verify",
        } or request.url.path.startswith(
            f"{settings.API_V1_STR}/notification-webhooks/"
        )
        if is_mutation and cookie_auth and not bearer_auth and not login_path:
            supplied = request.headers.get("origin")
            if supplied is None:
                referer = request.headers.get("referer")
                supplied = _origin(referer) if referer else None
            allowed = {_origin(settings.FRONTEND_HOST)}
            allowed.update(
                _origin(value.strip())
                for value in settings.BROWSER_TRUSTED_ORIGINS.split(",")
                if value.strip()
            )
            if supplied is None or _origin(supplied) not in allowed:
                response = JSONResponse(
                    status_code=403, content={"detail": "CSRF origin rejected"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(CookieCsrfMiddleware)


class SafeAccessLogMiddleware:
    """Emit an allowlisted access record without URLs, identifiers, or bodies.

    The default proxy/ASGI access formats include the raw request target. Patient
    search terms and opaque record identifiers therefore do not belong in them.
    Route *names* are assigned by the application and are safe to retain; raw
    paths, query strings, headers, cookies, request bodies and exception text are
    deliberately never read or logged here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        request_id = str(uuid.uuid4())
        status_code = 500
        response_started = False
        raw_method = str(scope.get("method", "UNKNOWN")).upper()
        method = raw_method if raw_method in _SAFE_HTTP_METHODS else "OTHER"

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # Stop exception values, request state, and raw server error pages at
            # the application boundary.  The response and log contain only a
            # stable machine code plus the generated correlation ID.
            if not response_started:
                status_code = 500
                response = JSONResponse(
                    status_code=500,
                    content={"detail": {"code": "INTERNAL_ERROR"}},
                )
                await response(scope, receive, send_with_request_id)
            else:
                access_logger.error(
                    "request_stream_failed request_id=%s code=INTERNAL_ERROR",
                    request_id,
                )
        finally:
            route = scope.get("route")
            route_name = getattr(route, "name", None)
            safe_route_name = route_name if isinstance(route_name, str) else "unmatched"
            duration_ms = round((time.perf_counter() - started) * 1_000)
            access_logger.info(
                "request_completed request_id=%s route=%s method=%s status=%s duration_ms=%s",
                request_id,
                safe_route_name,
                method,
                status_code,
                duration_ms,
            )
            try:
                record_operational_event(
                    request_id=request_id,
                    route=safe_route_name,
                    method=method,
                    status=status_code,
                    duration_ms=duration_ms,
                )
            except Exception:
                access_logger.error(
                    "operational_event_store_failed request_id=%s code=STORE_ERROR",
                    request_id,
                )


app.add_middleware(SafeAccessLogMiddleware)


def _merge_vary(existing: str | None, required: tuple[str, ...]) -> str:
    values: dict[str, str] = {}
    for value in (existing or "").split(","):
        normalized = value.strip()
        if normalized:
            values[normalized.lower()] = normalized
    for value in required:
        values[value.lower()] = value
    return ", ".join(values.values())


class PrivateResponseCacheMiddleware:
    """Prevent browsers and shared proxies from retaining care responses.

    This is deliberately a pure ASGI middleware rather than buffering or
    reconstructing a streamed response. Editing only ``http.response.start``
    preserves ``FileResponse``/SSE backpressure while applying the same headers
    to JSON, events, and the HTML shell.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))

        async def send_with_private_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                content_type = headers.get("content-type", "").lower()
                is_api = path.startswith(f"{settings.API_V1_STR}/")
                is_html_shell = content_type.startswith("text/html")
                if is_api or is_html_shell:
                    headers["Cache-Control"] = "private, no-store"
                    headers["Pragma"] = "no-cache"
                    headers["Vary"] = _merge_vary(
                        headers.get("Vary"),
                        ("Cookie", "Authorization", "Origin"),
                    )
                elif path.startswith("/assets/"):
                    # Vite filenames are content hashed; these files contain
                    # executable code and styles, never patient payloads.
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
            await send(message)

        await self.app(scope, receive, send_with_private_cache)


app.add_middleware(PrivateResponseCacheMiddleware)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    _request: Request, _exc: RequestValidationError
) -> JSONResponse:
    """Return no rejected values, free-form validator text, or field paths."""

    return JSONResponse(
        status_code=422,
        content={"detail": {"code": "REQUEST_VALIDATION_FAILED"}},
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

    @app.get("/login", include_in_schema=False)
    @app.get("/accept-invitation", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    @app.get("/my-care", include_in_schema=False)
    @app.get("/my-care/{spa_path:path}", include_in_schema=False)
    @app.get("/patients", include_in_schema=False)
    @app.get("/patients/{spa_path:path}", include_in_schema=False)
    @app.get("/patient", include_in_schema=False)
    @app.get("/patient/{spa_path:path}", include_in_schema=False)
    @app.get("/platform", include_in_schema=False)
    @app.get("/platform/{spa_path:path}", include_in_schema=False)
    async def frontend_route(spa_path: str = "") -> FileResponse:
        """Return the SPA shell for browser deep links and refreshes."""

        del spa_path
        return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html")

    app.frontend("/", directory=FRONTEND_DIR)
