import json

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse
from sqlmodel import col, select

from app.api.deps import CurrentContext, SessionDep
from app.models import DomainEvent

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/stream")
def event_stream(
    session: SessionDep,
    context: CurrentContext,
    last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    after = last_event_id or 0
    events = session.exec(
        select(DomainEvent)
        .where(
            DomainEvent.clinic_id == context.clinic_id,
            col(DomainEvent.sequence_no) > after,
        )
        .order_by(col(DomainEvent.sequence_no))
        .limit(200)
    ).all()
    frames = [
        "id: {id}\nevent: {event}\ndata: {data}\n\n".format(
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
        for event in events
    ]
    frames.append("event: caught-up\ndata: {}\n\n")
    return StreamingResponse(iter(frames), media_type="text/event-stream")
