from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

from websockets.asyncio.client import connect as websocket_connect


class LiveTranscriptionError(Exception):
    """A PHI-free live transport failure safe to expose as a reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LiveTranscriptEvent:
    kind: Literal["ready", "delta", "completed"]
    text: str = ""
    item_id: str | None = None


class LiveTranscriptionConnection(Protocol):
    provider_name: str
    model: str

    async def send_audio(self, pcm16: bytes) -> None: ...

    async def commit(self) -> None: ...

    async def receive_event(self) -> LiveTranscriptEvent: ...

    async def close(self) -> None: ...


class LiveTranscriptionProvider(Protocol):
    provider_name: str
    model: str

    async def connect(
        self, *, safety_identifier: str
    ) -> LiveTranscriptionConnection: ...


class RealtimeJSONTransport(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


TransportFactory = Callable[
    [str, dict[str, str], float], Awaitable[RealtimeJSONTransport]
]


async def _default_transport_factory(
    url: str, headers: dict[str, str], timeout_seconds: float
) -> RealtimeJSONTransport:
    connection = await websocket_connect(
        url,
        additional_headers=headers,
        open_timeout=timeout_seconds,
        close_timeout=5,
        max_size=256 * 1024,
        compression=None,
    )
    return cast(RealtimeJSONTransport, connection)


def _json_message(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class OpenAILiveTranscriptionConnection:
    provider_name = "openai-realtime"

    def __init__(
        self,
        *,
        transport: RealtimeJSONTransport,
        model: str,
        max_frame_bytes: int,
    ) -> None:
        self.transport = transport
        self.model = model
        self.max_frame_bytes = max_frame_bytes
        self._committed = False

    async def configure(self) -> None:
        await self.transport.send(
            _json_message(
                {
                    "type": "session.update",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24_000},
                                "transcription": {
                                    "model": self.model,
                                    "languages": ["en", "zh", "cmn"],
                                    "delay": "low",
                                },
                                "turn_detection": None,
                            }
                        },
                    },
                }
            )
        )

    async def send_audio(self, pcm16: bytes) -> None:
        if self._committed:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_ALREADY_COMMITTED")
        if not pcm16 or len(pcm16) % 2 != 0 or len(pcm16) > self.max_frame_bytes:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_FRAME_INVALID")
        await self.transport.send(
            _json_message(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm16).decode("ascii"),
                }
            )
        )

    async def commit(self) -> None:
        if self._committed:
            return
        self._committed = True
        await self.transport.send(_json_message({"type": "input_audio_buffer.commit"}))

    async def receive_event(self) -> LiveTranscriptEvent:
        while True:
            raw = await self.transport.recv()
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise LiveTranscriptionError(
                        "LIVE_TRANSCRIPT_PROVIDER_PROTOCOL_ERROR"
                    ) from exc
            try:
                payload: Any = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LiveTranscriptionError(
                    "LIVE_TRANSCRIPT_PROVIDER_PROTOCOL_ERROR"
                ) from exc
            if not isinstance(payload, dict):
                raise LiveTranscriptionError("LIVE_TRANSCRIPT_PROVIDER_PROTOCOL_ERROR")
            event_type = payload.get("type")
            if event_type == "session.updated":
                return LiveTranscriptEvent(kind="ready")
            if event_type == "conversation.item.input_audio_transcription.delta":
                delta = payload.get("delta")
                if not isinstance(delta, str):
                    raise LiveTranscriptionError(
                        "LIVE_TRANSCRIPT_PROVIDER_PROTOCOL_ERROR"
                    )
                return LiveTranscriptEvent(
                    kind="delta",
                    text=delta,
                    item_id=(
                        str(payload["item_id"]) if payload.get("item_id") else None
                    ),
                )
            if event_type == "conversation.item.input_audio_transcription.completed":
                transcript = payload.get("transcript")
                if not isinstance(transcript, str):
                    raise LiveTranscriptionError(
                        "LIVE_TRANSCRIPT_PROVIDER_PROTOCOL_ERROR"
                    )
                return LiveTranscriptEvent(
                    kind="completed",
                    text=transcript,
                    item_id=(
                        str(payload["item_id"]) if payload.get("item_id") else None
                    ),
                )
            if event_type == "error":
                # Provider messages may echo request context. Only expose a
                # stable local code; never forward or log the upstream value.
                raise LiveTranscriptionError("LIVE_TRANSCRIPT_PROVIDER_ERROR")

    async def close(self) -> None:
        await self.transport.close()


class OpenAILiveTranscriptionProvider:
    provider_name = "openai-realtime"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_frame_bytes: int,
        timeout_seconds: float,
        transport_factory: TransportFactory = _default_transport_factory,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.max_frame_bytes = max_frame_bytes
        self.timeout_seconds = timeout_seconds
        self.transport_factory = transport_factory

    async def connect(
        self, *, safety_identifier: str
    ) -> OpenAILiveTranscriptionConnection:
        url = f"wss://api.openai.com/v1/realtime?model={quote(self.model, safe='-_')}"
        try:
            transport = await self.transport_factory(
                url,
                {
                    "Authorization": f"Bearer {self.api_key}",
                    "OpenAI-Safety-Identifier": safety_identifier,
                },
                self.timeout_seconds,
            )
            connection = OpenAILiveTranscriptionConnection(
                transport=transport,
                model=self.model,
                max_frame_bytes=self.max_frame_bytes,
            )
            try:
                await connection.configure()
            except asyncio.CancelledError:
                # The route bounds provider setup with wait_for(). If that
                # deadline cancels session.update after the transport opened,
                # close the owned socket before preserving cancellation.
                with suppress(asyncio.CancelledError, Exception):
                    await transport.close()
                raise
            except Exception:
                # The factory has transferred ownership to this method. A
                # failed session.update must not leave an upstream audio
                # socket alive outside the connection lease.
                with suppress(Exception):
                    await transport.close()
                raise
            return connection
        except LiveTranscriptionError:
            raise
        except Exception as exc:
            raise LiveTranscriptionError(
                "LIVE_TRANSCRIPT_PROVIDER_UNAVAILABLE"
            ) from exc


class DeterministicLiveTranscriptionConnection:
    provider_name = "deterministic-synthetic-fixture"

    def __init__(self, *, model: str, max_frame_bytes: int) -> None:
        self.model = model
        self.max_frame_bytes = max_frame_bytes
        self._events: asyncio.Queue[LiveTranscriptEvent] = asyncio.Queue()
        self._pieces = iter(
            (
                "Patient reports a penicillin allergy. ",
                "医生会复核 medication and breathing difficulty.",
            )
        )
        self._text = ""
        self._committed = False
        self._closed = False
        self._events.put_nowait(LiveTranscriptEvent(kind="ready"))

    async def send_audio(self, pcm16: bytes) -> None:
        if self._closed or self._committed:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_ALREADY_COMMITTED")
        if not pcm16 or len(pcm16) % 2 != 0 or len(pcm16) > self.max_frame_bytes:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_FRAME_INVALID")
        piece = next(self._pieces, "")
        if piece:
            self._text += piece
            await self._events.put(
                LiveTranscriptEvent(
                    kind="delta", text=piece, item_id="synthetic-live-turn-1"
                )
            )

    async def commit(self) -> None:
        if self._committed:
            return
        self._committed = True
        await self._events.put(
            LiveTranscriptEvent(
                kind="completed",
                text=self._text.strip(),
                item_id="synthetic-live-turn-1",
            )
        )

    async def receive_event(self) -> LiveTranscriptEvent:
        if self._closed:
            raise LiveTranscriptionError("LIVE_TRANSCRIPT_PROVIDER_UNAVAILABLE")
        return await self._events.get()

    async def close(self) -> None:
        self._closed = True


class DeterministicLiveTranscriptionProvider:
    provider_name = "deterministic-synthetic-fixture"

    def __init__(self, *, fixture_id: str, max_frame_bytes: int) -> None:
        if fixture_id != "code-switch-overlap-v1":
            raise ValueError("Unknown synthetic live transcript fixture")
        self.model = fixture_id
        self.max_frame_bytes = max_frame_bytes

    async def connect(
        self, *, safety_identifier: str
    ) -> DeterministicLiveTranscriptionConnection:
        del safety_identifier
        return DeterministicLiveTranscriptionConnection(
            model=self.model, max_frame_bytes=self.max_frame_bytes
        )
