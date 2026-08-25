import json
import uuid
from asyncio import sleep
from collections.abc import AsyncIterator
from time import monotonic

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, col, select
from starlette.concurrency import run_in_threadpool

from app.api.deps import EventContext
from app.core.db import engine, set_rls_clinic
from app.models import ClinicMembership, DomainEvent, User

router = APIRouter(prefix="/events", tags=["events"])

POLL_INTERVAL_SECONDS = 1.0
HEARTBEAT_INTERVAL_SECONDS = 15.0


def _load_events(
    user_id: uuid.UUID,
    membership_id: uuid.UUID,
    clinic_id: uuid.UUID,
    after: int,
) -> list[DomainEvent] | None:
    """Reauthorize and load one page in the same short-lived transaction.

    A shared row lock prevents membership deactivation from committing between
    the active-membership check and the event read. Once revocation commits,
    every later poll returns ``None`` before reading tenant events.
    """

    with Session(engine) as session:
        set_rls_clinic(session, clinic_id)
        identity = session.exec(
            select(ClinicMembership, User)
            .join(User, col(User.id) == ClinicMembership.user_id)
            .where(
                ClinicMembership.id == membership_id,
                ClinicMembership.user_id == user_id,
                ClinicMembership.clinic_id == clinic_id,
            )
            .with_for_update(read=True)
        ).first()
        if identity is None:
            return None
        membership, user = identity
        if (
            not membership.is_active
            or not user.is_active
            or membership.role not in {"staff", "clinician"}
        ):
            return None
        return list(
            session.exec(
                select(DomainEvent)
                .where(
                    DomainEvent.clinic_id == clinic_id,
                    col(DomainEvent.sequence_no) > after,
                )
                .order_by(col(DomainEvent.sequence_no))
                .limit(200)
            ).all()
        )


def _event_frame(event: DomainEvent) -> str:
    return "id: {id}\nevent: {event}\ndata: {data}\n\n".format(
        id=event.sequence_no,
        event=event.event_type,
        data=json.dumps(
            {
                "aggregate_type": event.aggregate_type,
                "aggregate_id": str(event.aggregate_id),
                "payload": event.payload_json,
            },
            separators=(",", ":"),
        ),
    )


async def _frames(
    user_id: uuid.UUID,
    membership_id: uuid.UUID,
    clinic_id: uuid.UUID,
    after: int,
    *,
    snapshot: bool,
) -> AsyncIterator[str]:
    cursor = after
    last_heartbeat = monotonic()
    while True:
        events = await run_in_threadpool(
            _load_events, user_id, membership_id, clinic_id, cursor
        )
        if events is None:
            # Do not disclose why the context ended or include resource data.
            # The browser uses this terminal frame to start its cross-tab
            # session cleanup without waiting for a reconnect rejection.
            yield "event: session.revoked\ndata: {}\n\n"
            return
        for event in events:
            if event.sequence_no is None:
                continue
            cursor = event.sequence_no
            yield _event_frame(event)
        if snapshot:
            yield "event: caught-up\ndata: {}\n\n"
            return
        now = monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            yield ": heartbeat\n\n"
            last_heartbeat = now
        await sleep(POLL_INTERVAL_SECONDS)


@router.get("/stream")
def event_stream(
    context: EventContext,
    snapshot: bool = False,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical event role required")
    after = max(last_event_id or 0, 0)
    return StreamingResponse(
        _frames(
            context.user_id,
            context.membership.id,
            context.clinic_id,
            after,
            snapshot=snapshot,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
