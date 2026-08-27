from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import sentry_sdk
from fastapi import FastAPI, Request
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
from app.services.redaction import assert_presidio_runtime

FRONTEND_DIR = Path(__file__).parent / "frontend"


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0] if route.tags else 'frontend'}-{route.name}"


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
        }
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
