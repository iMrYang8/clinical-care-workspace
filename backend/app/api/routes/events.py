import json
import time
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.api.deps import CurrentContext
from app.core.db import engine
from app.models import DomainEvent

router = APIRouter(prefix="/events", tags=["events"])

POLL_INTERVAL_SECONDS = 1.0
HEARTBEAT_INTERVAL_SECONDS = 15.0


def _load_events(clinic_id: uuid.UUID, after: int) -> list[DomainEvent]:
    """Load one bounded event page in its own short-lived transaction."""

    with Session(engine) as session:
        if session.get_bind().dialect.name == "postgresql":
            session.connection().execute(
                text("SELECT set_config('app.current_clinic_id', :clinic_id, true)"),
                {"clinic_id": str(clinic_id)},
            )
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


def _frames(clinic_id: uuid.UUID, after: int, *, snapshot: bool) -> Iterator[str]:
    cursor = after
    last_heartbeat = time.monotonic()
    while True:
        events = _load_events(clinic_id, cursor)
        for event in events:
            if event.sequence_no is None:
                continue
            cursor = event.sequence_no
            yield _event_frame(event)
        if snapshot:
            yield "event: caught-up\ndata: {}\n\n"
            return
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            yield ": heartbeat\n\n"
            last_heartbeat = now
        time.sleep(POLL_INTERVAL_SECONDS)


@router.get("/stream")
def event_stream(
    context: CurrentContext,
    snapshot: bool = False,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    if context.role not in {"staff", "clinician"}:
        raise HTTPException(status_code=403, detail="Clinical event role required")
    after = max(last_event_id or 0, 0)
    return StreamingResponse(
        _frames(context.clinic_id, after, snapshot=snapshot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
