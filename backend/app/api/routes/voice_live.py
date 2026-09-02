from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Literal, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from sqlmodel import Session, col, select
from starlette.websockets import WebSocketState

from app.api.deps import (
    CurrentContext,
    RequestContext,
    SessionDep,
    resolve_request_context_token,
)
from app.core.config import settings
from app.core.db import engine, set_rls_actor, set_rls_clinic
from app.core.field_crypto import field_codec
from app.models import (
    Entry,
    EntryVersion,
    LiveTranscriptAvailability,
    LiveTranscriptStatus,
    ProvenancePointer,
    ProvisionalSafetyAlert,
    ProvisionalSafetyAlertPublic,
    ProvisionalSafetyAlertReviewRequest,
    get_datetime_utc,
)
from app.services.conflicts import detect_conflicts_for_assertion
from app.services.decisioning import create_assertion
from app.services.nightingale import emit_change
from app.services.voice.egress_policy import remote_audio_egress_denial
from app.services.voice.live import (
    configured_live_provider,
    live_availability,
    persist_completed_safety_alerts,
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


def _rate_limit_clock() -> float:
    return time.monotonic()


class _AudioRateLimiter:
    """Per-connection byte token bucket with one frame of burst headroom."""

    def __init__(
        self,
        *,
        bytes_per_second: int,
        max_frame_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = float(max(1, bytes_per_second))
        self.capacity = float(max_frame_bytes + max(1, bytes_per_second))
        self.tokens = self.capacity
        self.clock = clock
        self.updated_at = self.clock()

    def consume(self, size: int) -> bool:
        now = self.clock()
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
    token: str,
    session_id: uuid.UUID,
    *,
    require_recording: bool,
    require_remote_audio_egress: bool = False,
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
        if require_remote_audio_egress:
            denial = remote_audio_egress_denial(db, voice_session)
            if denial is not None:
                raise LiveTranscriptionError(denial)
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
        set_rls_actor(
            db,
            context.user_id,
            role=context.role,
            patient_id=context.linked_patient_id,
        )
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


def _persist_completed_alerts(
    context: RequestContext,
    session_id: uuid.UUID,
    *,
    source_event_id: str | None,
    text: str,
    source_language: str | None,
    completed_segment_at: datetime,
) -> list[uuid.UUID]:
    with Session(engine) as db:
        set_rls_clinic(db, context.clinic_id)
        set_rls_actor(
            db,
            context.user_id,
            role=context.role,
            patient_id=context.linked_patient_id,
        )
        voice_session = get_voice_session(db, context, session_id, lock=True)
        alerts = persist_completed_safety_alerts(
            db,
            context,
            voice_session,
            source_event_id=source_event_id,
            text=text,
            source_language=source_language,
            completed_segment_at=completed_segment_at,
        )
        db.commit()
        return [item.id for item in alerts]


def _alert_public(alert: ProvisionalSafetyAlert) -> ProvisionalSafetyAlertPublic:
    return ProvisionalSafetyAlertPublic(
        id=alert.id,
        patient_id=alert.patient_id,
        session_id=alert.session_id,
        source_event_id=alert.source_event_id,
        source_start_offset=alert.source_start_offset,
        source_end_offset=alert.source_end_offset,
        source_language=alert.source_language,
        concept_code=alert.concept_code,
        assertion_scope=alert.assertion_scope,
        polarity=alert.polarity,
        severity=alert.severity,
        state=cast(
            Literal["pending", "confirmed", "dismissed", "superseded"], alert.state
        ),
        completed_segment_at=alert.completed_segment_at,
        detected_at=alert.detected_at,
        reviewed_at=alert.reviewed_at,
        review_reason_code=alert.review_reason_code,
        confirmed_assertion_id=alert.confirmed_assertion_id,
    )


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
    return live_availability(voice_session, db=session)


@router.get(
    "/sessions/{session_id}/safety-alerts",
    response_model=list[ProvisionalSafetyAlertPublic],
)
def list_live_safety_alerts(
    session_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> list[ProvisionalSafetyAlertPublic]:
    if context.role not in {"staff", "clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Clinical team role required")
    voice_session = get_voice_session(session, context, session_id)
    rows = session.exec(
        select(ProvisionalSafetyAlert)
        .where(
            ProvisionalSafetyAlert.clinic_id == context.clinic_id,
            ProvisionalSafetyAlert.patient_id == voice_session.patient_id,
            ProvisionalSafetyAlert.session_id == voice_session.id,
        )
        .order_by(col(ProvisionalSafetyAlert.detected_at))
    ).all()
    return [_alert_public(item) for item in rows]


def _review_live_alert(
    alert_id: uuid.UUID,
    body: ProvisionalSafetyAlertReviewRequest,
    session: Session,
    context: RequestContext,
    *,
    confirm: bool,
) -> ProvisionalSafetyAlertPublic:
    if context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinician review required")
    alert = session.exec(
        select(ProvisionalSafetyAlert)
        .where(
            ProvisionalSafetyAlert.id == alert_id,
            ProvisionalSafetyAlert.clinic_id == context.clinic_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if alert is None:
        raise HTTPException(status_code=404, detail="Live safety alert not found")
    if alert.state == ("confirmed" if confirm else "dismissed"):
        return _alert_public(alert)
    if alert.state != "pending":
        raise HTTPException(
            status_code=409,
            detail={"code": "LIVE_SAFETY_ALERT_ALREADY_REVIEWED"},
        )
    reviewed_at = get_datetime_utc()
    if confirm:
        quote = field_codec.decrypt_text(
            alert.clinic_id,
            "provisional_safety_alert.source_text",
            alert.id,
            alert.source_text_ciphertext,
        )
        entry_id = uuid.uuid4()
        version_id = uuid.uuid4()
        entry = Entry(
            id=entry_id,
            clinic_id=context.clinic_id,
            patient_id=alert.patient_id,
            section="clinician",
            origin="human",
            entry_type="manual_clinician_note",
            patient_facing=False,
        )
        version = EntryVersion(
            id=version_id,
            clinic_id=context.clinic_id,
            entry_id=entry.id,
            version_no=1,
            title_ciphertext=field_codec.encrypt_text(
                context.clinic_id,
                "entry_version.title",
                version_id,
                "Confirmed live allergy alert",
            ),
            content_ciphertext=field_codec.encrypt_text(
                context.clinic_id,
                "entry_version.content",
                version_id,
                quote,
            ),
            content_sha256=hashlib.sha256(quote.encode()).hexdigest(),
            patient_facing=False,
            author_id=context.user_id,
        )
        session.add(entry)
        session.add(version)
        session.flush()
        entry.current_version_id = version.id
        session.add(entry)
        pointer_id = uuid.uuid4()
        pointer = ProvenancePointer(
            id=pointer_id,
            clinic_id=context.clinic_id,
            entry_version_id=version.id,
            start_offset=0,
            end_offset=len(quote),
            exact_quote_ciphertext=field_codec.encrypt_text(
                context.clinic_id,
                "provenance.exact_quote",
                pointer_id,
                quote,
            ),
            prefix_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "provenance.prefix", pointer_id, ""
            ),
            suffix_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "provenance.suffix", pointer_id, ""
            ),
            quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        )
        session.add(pointer)
        session.flush()
        assertion = create_assertion(
            session,
            clinic_id=context.clinic_id,
            patient_id=alert.patient_id,
            entry_id=entry.id,
            source_entry_version_id=version.id,
            provenance_pointer=pointer,
            fact_type="allergy",
            subject=alert.concept_code.removeprefix("allergy:"),
            normalized_value=alert.polarity,
            origin="human",
            polarity=alert.polarity,
            assertion_scope=alert.assertion_scope,
            source_language=alert.source_language,
            clinical_status=(
                "review_required"
                if alert.polarity == "unknown"
                or alert.concept_code == "allergy:review_required"
                else "active"
            ),
        )
        detect_conflicts_for_assertion(session, context, assertion)
        alert.state = "confirmed"
        alert.confirmed_assertion_id = assertion.id
    else:
        alert.state = "dismissed"
    alert.reviewed_by_membership_id = context.membership.id
    alert.reviewed_at = reviewed_at
    alert.review_reason_code = body.reason_code or (
        "clinician_confirmed" if confirm else "clinician_dismissed"
    )
    session.add(alert)
    emit_change(
        session,
        context,
        action=(
            "voice.provisional_safety_alert_confirmed"
            if confirm
            else "voice.provisional_safety_alert_dismissed"
        ),
        resource_type="provisional_safety_alert",
        resource_id=alert.id,
        metadata={"reason_code": alert.review_reason_code},
    )
    session.commit()
    session.refresh(alert)
    return _alert_public(alert)


@router.post(
    "/safety-alerts/{alert_id}/confirm",
    response_model=ProvisionalSafetyAlertPublic,
)
def confirm_live_safety_alert(
    alert_id: uuid.UUID,
    body: ProvisionalSafetyAlertReviewRequest,
    session: SessionDep,
    context: CurrentContext,
) -> ProvisionalSafetyAlertPublic:
    return _review_live_alert(alert_id, body, session, context, confirm=True)


@router.post(
    "/safety-alerts/{alert_id}/dismiss",
    response_model=ProvisionalSafetyAlertPublic,
)
def dismiss_live_safety_alert(
    alert_id: uuid.UUID,
    body: ProvisionalSafetyAlertReviewRequest,
    session: SessionDep,
    context: CurrentContext,
) -> ProvisionalSafetyAlertPublic:
    return _review_live_alert(alert_id, body, session, context, confirm=False)


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
            provider, reason, _, _ = configured_live_provider(voice_session, db=db)
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
                timeout=max(0.1, settings.REMOTE_CONNECT_TIMEOUT_SECONDS),
            )
        except TimeoutError as exc:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_CONNECT_TIMEOUT") from exc
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
    ready_deadline = time.monotonic() + max(
        0.1, settings.REMOTE_FIRST_RESULT_TIMEOUT_SECONDS
    )
    audio_started_at: float | None = None
    first_transcript_result = False
    last_output_at: float | None = None
    rate_limiter = _AudioRateLimiter(
        bytes_per_second=settings.LIVE_TRANSCRIPT_MAX_BYTES_PER_SECOND,
        max_frame_bytes=settings.LIVE_TRANSCRIPT_MAX_FRAME_BYTES,
        clock=_rate_limit_clock,
    )
    client_task = asyncio.create_task(websocket.receive())
    provider_task = asyncio.create_task(connection.receive_event())
    try:
        while True:
            now = time.monotonic()
            provider_deadline: tuple[float, str] | None = None
            if not provider_ready:
                provider_deadline = (
                    ready_deadline,
                    "LIVE_TRANSCRIPT_FIRST_RESULT_TIMEOUT",
                )
            elif audio_started_at is not None and not first_transcript_result:
                provider_deadline = (
                    audio_started_at
                    + max(0.1, settings.REMOTE_FIRST_RESULT_TIMEOUT_SECONDS),
                    "LIVE_TRANSCRIPT_FIRST_RESULT_TIMEOUT",
                )
            elif (
                first_transcript_result
                and not provider_completed
                and last_output_at is not None
            ):
                provider_deadline = (
                    last_output_at
                    + max(0.1, settings.LIVE_TRANSCRIPT_OUTPUT_SILENCE_SECONDS),
                    "LIVE_TRANSCRIPT_OUTPUT_SILENCE",
                )
            wait_timeout = max(
                0.01,
                settings.LIVE_TRANSCRIPT_FRAME_TIMEOUT_SECONDS,
            )
            if provider_deadline is not None:
                wait_timeout = min(wait_timeout, max(0.01, provider_deadline[0] - now))
            done, _ = await asyncio.wait(
                {client_task, provider_task},
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                if (
                    provider_deadline is not None
                    and time.monotonic() >= provider_deadline[0]
                ):
                    raise LiveTranscriptionError(provider_deadline[1])
                raise LiveTranscriptionError("LIVE_TRANSCRIPT_IDLE_TIMEOUT")

            if client_task in done:
                message = client_task.result()
                if message["type"] == "websocket.disconnect":
                    if not provider_completed:
                        reason_code = "LIVE_TRANSCRIPT_DISCONNECTED"
                    break
                payload = message.get("bytes")
                text = message.get("text")
                context = _authorized_context(
                    token,
                    session_id,
                    require_recording=not committed,
                    # Re-read clinic policy and the persisted session consent
                    # immediately before every PHI-bearing remote audio frame.
                    # A revocation therefore fences the next frame even while
                    # the upstream socket itself remains connected.
                    require_remote_audio_egress=(
                        payload is not None and connection.remote_audio_egress_required
                    ),
                )
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
                            timeout=max(0.1, settings.REMOTE_REQUEST_TIMEOUT_SECONDS),
                        )
                    except TimeoutError as exc:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_PROVIDER_TIMEOUT"
                        ) from exc
                    if audio_started_at is None:
                        audio_started_at = time.monotonic()
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
                            timeout=max(0.1, settings.REMOTE_REQUEST_TIMEOUT_SECONDS),
                        )
                    except TimeoutError as exc:
                        raise LiveTranscriptionError(
                            "LIVE_TRANSCRIPT_PROVIDER_TIMEOUT"
                        ) from exc
                    committed = True
                    if audio_started_at is None:
                        audio_started_at = time.monotonic()
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
                    alert_ids: list[uuid.UUID] = []
                    if event.kind == "completed":
                        # Capture the server-observed completion boundary before
                        # opening the persistence transaction. Alert latency is
                        # measured from this timestamp, not from row creation.
                        completed_segment_at = get_datetime_utc()
                        alert_ids = _persist_completed_alerts(
                            context,
                            session_id,
                            source_event_id=event.item_id,
                            text=event.text,
                            source_language=event.source_language,
                            completed_segment_at=completed_segment_at,
                        )
                    await _safe_send(
                        websocket,
                        {
                            "type": f"transcript.{event.kind}",
                            "text": event.text,
                            "item_id": event.item_id,
                            "source_language": event.source_language,
                            "provisional_alert_ids": [
                                str(alert_id) for alert_id in alert_ids
                            ],
                            "provisional": True,
                        },
                    )
                    first_transcript_result = True
                    last_output_at = time.monotonic()
                    if event.kind == "completed" and committed:
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
