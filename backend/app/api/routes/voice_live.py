from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import Session
from starlette.websockets import WebSocketState

from app.api.deps import (
    CurrentContext,
    RequestContext,
    SessionDep,
    resolve_request_context_token,
)
from app.core.config import settings
from app.core.db import engine, set_rls_clinic
from app.models import LiveTranscriptAvailability, LiveTranscriptStatus
from app.services.voice.live import (
    configured_live_provider,
    live_availability,
    safety_identifier,
    set_live_transcript_status,
)
from app.services.voice.live_limits import (
    LiveConnectionLease,
    LiveConnectionLimitError,
    live_connection_limiter,
)
from app.services.voice.live_providers import (
    LiveTranscriptionConnection,
    LiveTranscriptionError,
)
from app.services.voice.service import (
    authorize_voice_session_capture,
    get_voice_session,
)

router = APIRouter(prefix="/voice", tags=["voice"])


class _AudioRateLimiter:
    """Per-connection byte token bucket with one frame of burst headroom."""

    def __init__(self, *, bytes_per_second: int, max_frame_bytes: int) -> None:
        self.rate = float(max(1, bytes_per_second))
        self.capacity = float(max_frame_bytes + max(1, bytes_per_second))
        self.tokens = self.capacity
        self.updated_at = time.monotonic()

    def consume(self, size: int) -> bool:
        now = time.monotonic()
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if size > self.tokens:
            return False
        self.tokens -= size
        return True


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


def _trusted_origin(websocket: WebSocket) -> bool:
    supplied = websocket.headers.get("origin")
    if supplied is None:
        return False
    allowed = {_origin(settings.FRONTEND_HOST)}
    allowed.update(
        _origin(value.strip())
        for value in settings.BROWSER_TRUSTED_ORIGINS.split(",")
        if value.strip()
    )
    return _origin(supplied) in allowed


def _credential(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return websocket.cookies.get(settings.AUTH_COOKIE_NAME)


def _authorized_context(
    token: str, session_id: uuid.UUID, *, require_recording: bool
) -> RequestContext:
    with Session(engine) as db:
        context = resolve_request_context_token(db, token)
        voice_session = get_voice_session(db, context, session_id)
        authorize_voice_session_capture(context, voice_session)
        if require_recording and voice_session.state != "recording":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VOICE_SESSION_NOT_RECORDING",
                    "state": voice_session.state,
                },
            )
        db.expunge(context.user)
        db.expunge(context.membership)
        return context


def _persist_status(
    context: RequestContext,
    session_id: uuid.UUID,
    *,
    status: LiveTranscriptStatus,
    reason_code: str | None,
) -> LiveTranscriptStatus:
    with Session(engine) as db:
        set_rls_clinic(db, context.clinic_id)
        voice_session = get_voice_session(db, context, session_id, lock=True)
        persisted_status = set_live_transcript_status(
            db,
            context,
            voice_session,
            status=status,
            reason_code=reason_code,
        )
        db.commit()
        return persisted_status


async def _safe_send(websocket: WebSocket, payload: dict[str, object]) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        try:
            await asyncio.wait_for(websocket.send_json(payload), timeout=5.0)
        except TimeoutError as exc:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_CLIENT_BACKPRESSURE") from exc


async def _close(websocket: WebSocket, code: int) -> None:
    if websocket.application_state != WebSocketState.DISCONNECTED:
        with suppress(RuntimeError):
            await websocket.close(code=code)


@router.get("/sessions/{session_id}/live", response_model=LiveTranscriptAvailability)
def live_status(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> LiveTranscriptAvailability:
    voice_session = get_voice_session(session, context, session_id)
    try:
        authorize_voice_session_capture(context, voice_session)
    except HTTPException:
        return LiveTranscriptAvailability(
            available=False,
            status="unavailable",
            reason_code="ROLE_NOT_PERMITTED",
        )
    return live_availability(voice_session)


@router.websocket("/sessions/{session_id}/live/ws")
async def live_transcript_socket(websocket: WebSocket, session_id: uuid.UUID) -> None:
    if not _trusted_origin(websocket):
        await _close(websocket, 4403)
        return
    token = _credential(websocket)
    if token is None:
        await _close(websocket, 4401)
        return

    lease: LiveConnectionLease | None = None
    try:
        context = _authorized_context(token, session_id, require_recording=False)
        with Session(engine) as db:
            fresh_context = resolve_request_context_token(db, token)
            voice_session = get_voice_session(db, fresh_context, session_id, lock=True)
            authorize_voice_session_capture(fresh_context, voice_session)
            provider, reason, _, _ = configured_live_provider(voice_session)
            if provider is None:
                response_status: LiveTranscriptStatus = (
                    "replaced"
                    if reason == "FINAL_TRANSCRIPT_AVAILABLE"
                    else "unavailable"
                )
                if response_status != "replaced":
                    set_live_transcript_status(
                        db,
                        fresh_context,
                        voice_session,
                        status="unavailable",
                        reason_code=reason,
                    )
                db.commit()
                await websocket.accept()
                await _safe_send(
                    websocket,
                    {
                        "type": "status",
                        "status": response_status,
                        "reason_code": reason,
                        "provisional": True,
                        "needs_review": False,
                    },
                )
                await _close(websocket, 1000 if response_status == "replaced" else 4404)
                return
        await websocket.accept()
        try:
            lease = await live_connection_limiter.acquire(context, session_id)
        except LiveConnectionLimitError as exc:
            await _safe_send(
                websocket,
                {
                    "type": "status",
                    "status": "unavailable",
                    "reason_code": exc.code,
                    "provisional": True,
                    "needs_review": False,
                },
            )
            await _close(websocket, 4429)
            return
        try:
            connection = await asyncio.wait_for(
                provider.connect(safety_identifier=safety_identifier(context)),
                timeout=max(1.0, settings.LIVE_TRANSCRIPT_PROVIDER_TIMEOUT_SECONDS),
            )
        except TimeoutError as exc:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_PROVIDER_TIMEOUT") from exc
    except HTTPException as exc:
        if lease is not None:
            lease.release()
        await _close(websocket, 4404 if exc.status_code == 404 else 4403)
        return
    except LiveTranscriptionError as exc:
        if lease is not None:
            lease.release()
        persisted_status: LiveTranscriptStatus = "needs_review"
        with suppress(Exception):
            persisted_status = _persist_status(
                context,
                session_id,
                status="needs_review",
                reason_code=exc.code,
            )
        await _safe_send(
            websocket,
            {
                "type": "status",
                "status": persisted_status,
                "reason_code": (
                    "FINAL_TRANSCRIPT_AVAILABLE"
                    if persisted_status == "replaced"
                    else exc.code
                ),
                "provisional": True,
                "needs_review": persisted_status == "needs_review",
            },
        )
        await _close(websocket, 1000 if persisted_status == "replaced" else 1011)
        return
    except asyncio.CancelledError:
        if lease is not None:
            lease.release()
        with suppress(Exception):
            _persist_status(
                context,
                session_id,
                status="needs_review",
                reason_code="LIVE_TRANSCRIPT_DISCONNECTED",
            )
        raise
    except Exception:
        if lease is not None:
            lease.release()
        # Credential parsing, database failures, and provider details stay out
        # of the client payload and application logs.
        await _close(websocket, 1011)
        return

    try:
        await _run_live_session(websocket, connection, token, context, session_id)
    finally:
        if lease is not None:
            lease.release()


async def _run_live_session(
    websocket: WebSocket,
    connection: LiveTranscriptionConnection,
    token: str,
    context: RequestContext,
    session_id: uuid.UUID,
) -> None:
    total_bytes = 0
    committed = False
    provider_ready = False
    provider_completed = False
    reason_code: str | None = None
    rate_limiter = _AudioRateLimiter(
        bytes_per_second=settings.LIVE_TRANSCRIPT_MAX_BYTES_PER_SECOND,
        max_frame_bytes=settings.LIVE_TRANSCRIPT_MAX_FRAME_BYTES,
    )
    client_task = asyncio.create_task(websocket.receive())
    provider_task = asyncio.create_task(connection.receive_event())
    try:
        while True:
            done, _ = await asyncio.wait(
                {client_task, provider_task},
                timeout=max(
                    1.0,
                    (
                        settings.LIVE_TRANSCRIPT_FRAME_TIMEOUT_SECONDS
                        if provider_ready
                        else settings.LIVE_TRANSCRIPT_PROVIDER_TIMEOUT_SECONDS
                    ),
                ),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise LiveTranscriptionError(
                    "LIVE_TRANSCRIPT_IDLE_TIMEOUT"
                    if provider_ready
                    else "LIVE_TRANSCRIPT_PROVIDER_TIMEOUT"
                )

            if client_task in done:
                message = client_task.result()
                if message["type"] == "websocket.disconnect":
                    if not provider_completed:
                        reason_code = "LIVE_TRANSCRIPT_DISCONNECTED"
                    break
                context = _authorized_context(
                    token, session_id, require_recording=not committed
                )
                payload = message.get("bytes")
                text = message.get("text")
                if not provider_ready:
                    raise LiveTranscriptionError("LIVE_TRANSCRIPT_PROVIDER_NOT_READY")
                if payload is not None:
                    if committed:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_ALREADY_COMMITTED"
                        )
                    frame = bytes(payload)
                    if (
                        not frame
                        or len(frame) % 2 != 0
                        or len(frame) > settings.LIVE_TRANSCRIPT_MAX_FRAME_BYTES
                    ):
                        raise LiveTranscriptionError("LIVE_TRANSCRIPT_FRAME_INVALID")
                    total_bytes += len(frame)
                    if total_bytes > settings.LIVE_TRANSCRIPT_MAX_SESSION_BYTES:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_SESSION_LIMIT_REACHED"
                        )
                    if not rate_limiter.consume(len(frame)):
                        raise LiveTranscriptionError("LIVE_TRANSCRIPT_RATE_LIMIT")
                    try:
                        await asyncio.wait_for(
                            connection.send_audio(frame),
                            timeout=max(
                                1.0,
                                settings.LIVE_TRANSCRIPT_PROVIDER_TIMEOUT_SECONDS,
                            ),
                        )
                    except TimeoutError as exc:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_PROVIDER_TIMEOUT"
                        ) from exc
                elif text is not None:
                    try:
                        command = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_PROTOCOL_ERROR"
                        ) from exc
                    if command != {"type": "commit"}:
                        raise LiveTranscriptionError("LIVE_TRANSCRIPT_PROTOCOL_ERROR")
                    try:
                        await asyncio.wait_for(
                            connection.commit(),
                            timeout=max(
                                1.0,
                                settings.LIVE_TRANSCRIPT_PROVIDER_TIMEOUT_SECONDS,
                            ),
                        )
                    except TimeoutError as exc:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_PROVIDER_TIMEOUT"
                        ) from exc
                    committed = True
                else:
                    raise LiveTranscriptionError("LIVE_TRANSCRIPT_PROTOCOL_ERROR")
                client_task = asyncio.create_task(websocket.receive())

            if provider_task in done:
                event = provider_task.result()
                context = _authorized_context(
                    token, session_id, require_recording=not committed
                )
                if event.kind == "ready":
                    provider_ready = True
                    ready_status = _persist_status(
                        context,
                        session_id,
                        status="available",
                        reason_code=None,
                    )
                    if ready_status == "replaced":
                        await _safe_send(
                            websocket,
                            {
                                "type": "status",
                                "status": "replaced",
                                "reason_code": "FINAL_TRANSCRIPT_AVAILABLE",
                                "provisional": True,
                                "needs_review": False,
                            },
                        )
                        return
                    await _safe_send(
                        websocket,
                        {
                            "type": "status",
                            "status": "available",
                            "provider": connection.provider_name,
                            "model": connection.model,
                            "provisional": True,
                            "needs_review": False,
                        },
                    )
                else:
                    if not provider_ready:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_PROVIDER_PROTOCOL_ERROR"
                        )
                    await _safe_send(
                        websocket,
                        {
                            "type": f"transcript.{event.kind}",
                            "text": event.text,
                            "item_id": event.item_id,
                            "provisional": True,
                        },
                    )
                    if event.kind == "completed":
                        provider_completed = True
                provider_task = asyncio.create_task(connection.receive_event())
    except asyncio.CancelledError:
        # ASGI servers may cancel a socket task instead of delivering an
        # explicit disconnect during client teardown or process shutdown.
        # Preserve the incomplete-stream review state before propagating the
        # cancellation to the server.
        if not provider_completed:
            reason_code = "LIVE_TRANSCRIPT_DISCONNECTED"
        raise
    except WebSocketDisconnect:
        if not provider_completed:
            reason_code = "LIVE_TRANSCRIPT_DISCONNECTED"
    except LiveTranscriptionError as exc:
        reason_code = exc.code
    except HTTPException:
        reason_code = "LIVE_TRANSCRIPT_AUTHORIZATION_EXPIRED"
    except Exception:
        reason_code = "LIVE_TRANSCRIPT_PROVIDER_UNAVAILABLE"
    finally:
        for task in (client_task, provider_task):
            task.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await task
        with suppress(Exception):
            await asyncio.wait_for(connection.close(), timeout=5.0)
        if reason_code:
            persisted_status: LiveTranscriptStatus = "needs_review"
            with suppress(Exception):
                persisted_status = _persist_status(
                    context,
                    session_id,
                    status="needs_review",
                    reason_code=reason_code,
                )
            with suppress(asyncio.CancelledError, Exception):
                await _safe_send(
                    websocket,
                    {
                        "type": "status",
                        "status": persisted_status,
                        "reason_code": (
                            "FINAL_TRANSCRIPT_AVAILABLE"
                            if persisted_status == "replaced"
                            else reason_code
                        ),
                        "provisional": True,
                        "needs_review": persisted_status == "needs_review",
                    },
                )
            await _close(websocket, 1000 if persisted_status == "replaced" else 1011)
        else:
            await _close(websocket, 1000)
