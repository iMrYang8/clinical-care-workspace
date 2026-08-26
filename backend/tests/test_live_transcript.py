import asyncio
import json
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from app.api.deps import RequestContext
from app.api.routes import voice_live
from app.core.config import settings
from app.core.db import engine
from app.models import ClinicMembership, User, VoiceSession
from app.seed import demo_id
from app.services.voice.live import configured_live_provider
from app.services.voice.live_limits import (
    LiveConnectionLimiter,
    LiveConnectionLimitError,
)
from app.services.voice.live_providers import (
    LiveTranscriptEvent,
    LiveTranscriptionError,
    OpenAILiveTranscriptionProvider,
    RealtimeJSONTransport,
)


class _FakeRealtimeTransport:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.sent: list[dict[str, Any]] = []
        self.events = asyncio.Queue[str | bytes]()
        for event in events:
            self.events.put_nowait(json.dumps(event))
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str | bytes:
        return await self.events.get()

    async def close(self) -> None:
        self.closed = True


class _ConfigureFailureTransport(_FakeRealtimeTransport):
    async def send(self, _message: str) -> None:
        raise RuntimeError("upstream detail containing synthetic patient text")


class _BlockingConfigureTransport(_FakeRealtimeTransport):
    async def send(self, _message: str) -> None:
        await asyncio.Future()


class _NeverCompletesConnection:
    provider_name = "mock-never-completes"
    model = "mock-transport-only"

    def __init__(self) -> None:
        self._ready = True

    async def send_audio(self, _pcm16: bytes) -> None:
        return

    async def commit(self) -> None:
        return

    async def receive_event(self) -> LiveTranscriptEvent:
        if self._ready:
            self._ready = False
            return LiveTranscriptEvent(kind="ready")
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return


class _NeverCompletesProvider:
    provider_name = _NeverCompletesConnection.provider_name
    model = _NeverCompletesConnection.model

    async def connect(self, *, safety_identifier: str) -> _NeverCompletesConnection:
        assert safety_identifier
        return _NeverCompletesConnection()


@pytest.mark.unit
def test_openai_live_adapter_maps_official_realtime_protocol() -> None:
    transport = _FakeRealtimeTransport(
        [
            {"type": "session.updated"},
            {
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "item-1",
                "delta": "penicillin ",
            },
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "penicillin allergy",
            },
        ]
    )
    observed: dict[str, object] = {}

    async def factory(
        url: str, headers: dict[str, str], timeout: float
    ) -> RealtimeJSONTransport:
        observed.update(url=url, headers=headers, timeout=timeout)
        return transport

    async def run() -> None:
        provider = OpenAILiveTranscriptionProvider(
            api_key="TOKEN",
            model="gpt-live-transcribe",
            max_frame_bytes=16,
            timeout_seconds=7,
            transport_factory=factory,
        )
        connection = await provider.connect(safety_identifier="HASHED_SUBJECT")
        assert (await connection.receive_event()).kind == "ready"
        await connection.send_audio(b"\x00\x01\x02\x03")
        delta = await connection.receive_event()
        assert (delta.kind, delta.text, delta.item_id) == (
            "delta",
            "penicillin ",
            "item-1",
        )
        await connection.commit()
        completed = await connection.receive_event()
        assert (completed.kind, completed.text) == (
            "completed",
            "penicillin allergy",
        )
        await connection.close()

    asyncio.run(run())

    assert observed["url"] == (
        "wss://api.openai.com/v1/realtime?model=gpt-live-transcribe"
    )
    assert observed["headers"] == {
        "Authorization": "Bearer TOKEN",
        "OpenAI-Safety-Identifier": "HASHED_SUBJECT",
    }
    session_update = transport.sent[0]
    transcription = session_update["session"]["audio"]["input"]["transcription"]
    assert transcription == {
        "model": "gpt-live-transcribe",
        "languages": ["en", "zh", "cmn"],
        "delay": "low",
    }
    assert transport.sent[1] == {
        "type": "input_audio_buffer.append",
        "audio": "AAECAw==",
    }
    assert transport.sent[2] == {"type": "input_audio_buffer.commit"}
    assert transport.closed is True


@pytest.mark.unit
def test_openai_live_provider_requires_every_audio_egress_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_session = VoiceSession(
        clinic_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        capture_kind="clinical",
        state="recording",
        created_by_id=uuid.uuid4(),
    )
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_PROVIDER", "openai")
    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", True)
    monkeypatch.setattr(settings, "STRICT_NO_AUDIO_EGRESS", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "TOKEN")
    monkeypatch.setattr(settings, "OPENAI_LIVE_TRANSCRIBE_MODEL", "gpt-live-transcribe")

    provider, reason, _, _ = configured_live_provider(voice_session)
    assert provider is None
    assert reason == "STRICT_NO_AUDIO_EGRESS"

    monkeypatch.setattr(settings, "STRICT_NO_AUDIO_EGRESS", False)
    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", False)
    provider, reason, _, _ = configured_live_provider(voice_session)
    assert provider is None
    assert reason == "REMOTE_AUDIO_EGRESS_DISABLED"

    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_LIVE_TRANSCRIBE_MODEL", "wrong-model")
    provider, reason, _, _ = configured_live_provider(voice_session)
    assert provider is None
    assert reason == "OPENAI_LIVE_TRANSCRIPT_MODEL_UNSUPPORTED"

    monkeypatch.setattr(settings, "OPENAI_LIVE_TRANSCRIBE_MODEL", "gpt-live-transcribe")
    provider, reason, provider_name, model = configured_live_provider(voice_session)
    assert provider is not None
    assert reason is None
    assert provider_name == "openai-realtime"
    assert model == "gpt-live-transcribe"


@pytest.mark.unit
def test_live_provider_does_not_open_before_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_session = VoiceSession(
        clinic_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        capture_kind="clinical",
        state="created",
        created_by_id=uuid.uuid4(),
        synthetic_fixture=True,
        fixture_id="code-switch-overlap-v1",
    )
    _enable_deterministic_live(monkeypatch)

    provider, reason, _, _ = configured_live_provider(voice_session)
    assert provider is None
    assert reason == "VOICE_SESSION_NOT_RECORDING"


@pytest.mark.unit
def test_openai_live_adapter_redacts_provider_error_detail() -> None:
    transport = _FakeRealtimeTransport(
        [
            {
                "type": "error",
                "error": {
                    "message": "Patient Alice payload was rejected",
                    "code": "raw-upstream-code",
                },
            }
        ]
    )

    async def factory(
        _url: str, _headers: dict[str, str], _timeout: float
    ) -> RealtimeJSONTransport:
        return transport

    async def run() -> None:
        connection = await OpenAILiveTranscriptionProvider(
            api_key="TOKEN",
            model="gpt-live-transcribe",
            max_frame_bytes=16,
            timeout_seconds=7,
            transport_factory=factory,
        ).connect(safety_identifier="HASHED_SUBJECT")
        with pytest.raises(LiveTranscriptionError) as raised:
            await connection.receive_event()
        assert str(raised.value) == "LIVE_TRANSCRIPT_PROVIDER_ERROR"
        assert "Alice" not in str(raised.value)

    asyncio.run(run())


@pytest.mark.unit
def test_openai_live_adapter_closes_transport_when_configuration_fails() -> None:
    transport = _ConfigureFailureTransport([])

    async def factory(
        _url: str, _headers: dict[str, str], _timeout: float
    ) -> RealtimeJSONTransport:
        return transport

    async def run() -> None:
        provider = OpenAILiveTranscriptionProvider(
            api_key="TOKEN",
            model="gpt-live-transcribe",
            max_frame_bytes=16,
            timeout_seconds=7,
            transport_factory=factory,
        )
        with pytest.raises(LiveTranscriptionError) as raised:
            await provider.connect(safety_identifier="HASHED_SUBJECT")
        assert str(raised.value) == "LIVE_TRANSCRIPT_PROVIDER_UNAVAILABLE"
        assert "patient" not in str(raised.value).lower()

    asyncio.run(run())
    assert transport.closed is True


@pytest.mark.unit
def test_openai_live_adapter_closes_transport_when_setup_is_cancelled() -> None:
    transport = _BlockingConfigureTransport([])

    async def factory(
        _url: str, _headers: dict[str, str], _timeout: float
    ) -> RealtimeJSONTransport:
        return transport

    async def run() -> None:
        provider = OpenAILiveTranscriptionProvider(
            api_key="TOKEN",
            model="gpt-live-transcribe",
            max_frame_bytes=16,
            timeout_seconds=7,
            transport_factory=factory,
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                provider.connect(safety_identifier="HASHED_SUBJECT"),
                timeout=0.01,
            )

    asyncio.run(run())
    assert transport.closed is True


def _patient(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]


def _synthetic_recording(
    client: TestClient, headers: dict[str, str]
) -> tuple[str, str]:
    patient_id = _patient(client, headers)
    created = client.post(
        "/api/v1/voice/sessions",
        headers=headers,
        json={
            "patient_id": patient_id,
            "capture_kind": "clinical",
            "synthetic_fixture": True,
            "fixture_id": "code-switch-overlap-v1",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    joined = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=headers,
        json={
            "client_device_id": "live-browser",
            "capture_role": "patient",
            "expected_patient_id": patient_id,
            "expected_capture_kind": "clinical",
        },
    )
    assert joined.status_code == 201, joined.text
    return session_id, patient_id


def _enable_deterministic_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_PROVIDER", "deterministic")


def test_live_websocket_is_authenticated_scoped_and_provisional(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_deterministic_live(monkeypatch)
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)

    with client.websocket_connect(
        f"/api/v1/voice/sessions/{session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        status = socket.receive_json()
        assert status["status"] == "available"
        assert status["provisional"] is True
        assert status["provider"] == "deterministic-synthetic-fixture"
        socket.send_bytes(b"\x00\x00" * 100)
        delta = socket.receive_json()
        assert delta["type"] == "transcript.delta"
        assert delta["provisional"] is True
        assert "penicillin" in delta["text"]
        socket.send_text('{"type":"commit"}')
        completed = socket.receive_json()
        assert completed["type"] == "transcript.completed"
        assert completed["provisional"] is True

    session = client.get(
        f"/api/v1/voice/sessions/{session_id}", headers=clinician
    ).json()
    assert session["live_transcript_status"] == "available"
    assert session["current_transcript_revision_id"] is None


def test_live_websocket_rejects_cross_clinic_and_untrusted_origin(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_deterministic_live(monkeypatch)
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)

    with pytest.raises(WebSocketDisconnect) as cross_clinic:
        with client.websocket_connect(
            f"/api/v1/voice/sessions/{session_id}/live/ws",
            headers={
                **auth_headers("other_staff"),
                "Origin": str(settings.FRONTEND_HOST),
            },
        ):
            pass
    assert cross_clinic.value.code == 4404

    with pytest.raises(WebSocketDisconnect) as bad_origin:
        with client.websocket_connect(
            f"/api/v1/voice/sessions/{session_id}/live/ws",
            headers={**clinician, "Origin": "https://evil.invalid"},
        ):
            pass
    assert bad_origin.value.code == 4403


def test_live_frame_bound_persists_needs_review_without_audio_detail(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_deterministic_live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_MAX_FRAME_BYTES", 4)
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)

    with client.websocket_connect(
        f"/api/v1/voice/sessions/{session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        assert socket.receive_json()["status"] == "available"
        socket.send_bytes(b"\x00\x00" * 3)
        needs_review = socket.receive_json()
        assert needs_review["status"] == "needs_review"
        assert needs_review["reason_code"] == "LIVE_TRANSCRIPT_FRAME_INVALID"

    session: dict[str, Any] = {}
    for _ in range(100):
        session = client.get(
            f"/api/v1/voice/sessions/{session_id}", headers=clinician
        ).json()
        if session["live_transcript_status"] == "needs_review":
            break
        time.sleep(0.01)
    assert session["live_transcript_status"] == "needs_review"
    assert session["live_transcript_reason_code"] == "LIVE_TRANSCRIPT_FRAME_INVALID"
    assert "audio" not in json.dumps(session).lower()


def test_live_stream_enforces_total_bytes_and_accelerated_replay_rate(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_deterministic_live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_MAX_FRAME_BYTES", 4)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_MAX_SESSION_BYTES", 32)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_MAX_BYTES_PER_SECOND", 4)
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)

    with client.websocket_connect(
        f"/api/v1/voice/sessions/{session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        assert socket.receive_json()["status"] == "available"
        socket.send_bytes(b"\x00\x00" * 2)
        assert socket.receive_json()["type"] == "transcript.delta"
        socket.send_bytes(b"\x00\x00" * 2)
        assert socket.receive_json()["type"] == "transcript.delta"
        socket.send_bytes(b"\x00\x00" * 2)
        limited = socket.receive_json()
        assert limited["status"] == "needs_review"
        assert limited["reason_code"] == "LIVE_TRANSCRIPT_RATE_LIMIT"

    # A separate stream reaches its per-connection aggregate cap independently
    # of the byte-rate bucket.
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_MAX_SESSION_BYTES", 4)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_MAX_BYTES_PER_SECOND", 1_000_000)
    capped_session_id, _ = _synthetic_recording(client, clinician)
    with client.websocket_connect(
        f"/api/v1/voice/sessions/{capped_session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        assert socket.receive_json()["status"] == "available"
        socket.send_bytes(b"\x00\x00" * 2)
        assert socket.receive_json()["type"] == "transcript.delta"
        socket.send_bytes(b"\x00\x00" * 2)
        limited = socket.receive_json()
        assert limited["reason_code"] == "LIVE_TRANSCRIPT_SESSION_LIMIT_REACHED"


def test_disconnect_after_commit_before_provider_completion_requires_review(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)
    provider = _NeverCompletesProvider()
    monkeypatch.setattr(
        voice_live,
        "configured_live_provider",
        lambda _voice_session: (
            provider,
            None,
            provider.provider_name,
            provider.model,
        ),
    )

    with client.websocket_connect(
        f"/api/v1/voice/sessions/{session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        assert socket.receive_json()["status"] == "available"
        socket.send_text('{"type":"commit"}')

    session: dict[str, Any] = {}
    for _ in range(100):
        session = client.get(
            f"/api/v1/voice/sessions/{session_id}", headers=clinician
        ).json()
        if session["live_transcript_status"] == "needs_review":
            break
        time.sleep(0.01)
    assert session["live_transcript_status"] == "needs_review"
    assert session["live_transcript_reason_code"] == "LIVE_TRANSCRIPT_DISCONNECTED"


def test_live_stream_rechecks_membership_after_connection(
    client: TestClient,
    owner_session: Session,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_deterministic_live(monkeypatch)
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)

    with client.websocket_connect(
        f"/api/v1/voice/sessions/{session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        assert socket.receive_json()["status"] == "available"
        membership = owner_session.get(
            ClinicMembership, demo_id("membership-clinician")
        )
        assert membership is not None
        membership.is_active = False
        owner_session.add(membership)
        owner_session.commit()

        socket.send_bytes(b"\x00\x00")
        review = socket.receive_json()
        assert review["status"] == "needs_review"
        assert review["reason_code"] == "LIVE_TRANSCRIPT_AUTHORIZATION_EXPIRED"


def _mark_live_replaced(owner_session: Session, session_id: str) -> None:
    voice_session = owner_session.get(VoiceSession, uuid.UUID(session_id))
    assert voice_session is not None
    voice_session.state = "ready"
    voice_session.live_transcript_status = "replaced"
    voice_session.live_transcript_error_code = None
    owner_session.add(voice_session)
    owner_session.commit()


def test_final_transcript_replaced_is_a_terminal_reconnect_state(
    client: TestClient,
    owner_session: Session,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_deterministic_live(monkeypatch)
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)
    _mark_live_replaced(owner_session, session_id)

    with client.websocket_connect(
        f"/api/v1/voice/sessions/{session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        assert socket.receive_json() == {
            "type": "status",
            "status": "replaced",
            "reason_code": "FINAL_TRANSCRIPT_AVAILABLE",
            "provisional": True,
            "needs_review": False,
        }

    response = client.get(f"/api/v1/voice/sessions/{session_id}", headers=clinician)
    assert response.json()["live_transcript_status"] == "replaced"


def test_late_socket_failure_cannot_downgrade_final_replaced_state(
    client: TestClient,
    owner_session: Session,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_deterministic_live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_MAX_FRAME_BYTES", 4)
    clinician = auth_headers("clinician")
    session_id, _ = _synthetic_recording(client, clinician)

    with client.websocket_connect(
        f"/api/v1/voice/sessions/{session_id}/live/ws",
        headers={**clinician, "Origin": str(settings.FRONTEND_HOST)},
    ) as socket:
        assert socket.receive_json()["status"] == "available"
        # Simulate the final worker committing its immutable revision while a
        # previously authorized live socket is still open.
        _mark_live_replaced(owner_session, session_id)
        socket.send_bytes(b"\x00\x00" * 3)
        assert socket.receive_json() == {
            "type": "status",
            "status": "replaced",
            "reason_code": "FINAL_TRANSCRIPT_AVAILABLE",
            "provisional": True,
            "needs_review": False,
        }

    response = client.get(f"/api/v1/voice/sessions/{session_id}", headers=clinician)
    assert response.json()["live_transcript_status"] == "replaced"
    assert response.json()["live_transcript_reason_code"] is None


def _demo_context(owner_session: Session, persona: str) -> RequestContext:
    user = owner_session.get(User, demo_id(f"user-{persona}"))
    membership = owner_session.get(ClinicMembership, demo_id(f"membership-{persona}"))
    assert user is not None
    assert membership is not None
    owner_session.expunge(user)
    owner_session.expunge(membership)
    return RequestContext(user=user, membership=membership)


def test_live_connection_leases_bound_global_clinic_user_and_session(
    owner_session: Session,
) -> None:
    staff = _demo_context(owner_session, "staff")
    clinician = _demo_context(owner_session, "clinician")

    async def assert_limit(
        limiter: LiveConnectionLimiter,
        context: RequestContext,
        session_id: uuid.UUID,
        expected: str,
    ) -> None:
        with pytest.raises(LiveConnectionLimitError) as raised:
            await limiter.acquire(context, session_id)
        assert raised.value.code == expected

    async def run() -> None:
        first_session = uuid.uuid4()
        second_session = uuid.uuid4()

        global_limiter = LiveConnectionLimiter(
            db_engine=engine,
            max_global=1,
            max_clinic=4,
            max_user=4,
            timeout_seconds=0.02,
        )
        lease = await global_limiter.acquire(staff, first_session)
        try:
            await assert_limit(
                global_limiter,
                clinician,
                second_session,
                "LIVE_TRANSCRIPT_GLOBAL_LIMIT",
            )
        finally:
            lease.release()

        clinic_limiter = LiveConnectionLimiter(
            db_engine=engine,
            max_global=2,
            max_clinic=1,
            max_user=2,
            timeout_seconds=0.02,
        )
        lease = await clinic_limiter.acquire(staff, first_session)
        try:
            await assert_limit(
                clinic_limiter,
                clinician,
                second_session,
                "LIVE_TRANSCRIPT_CLINIC_LIMIT",
            )
        finally:
            lease.release()

        user_limiter = LiveConnectionLimiter(
            db_engine=engine,
            max_global=2,
            max_clinic=2,
            max_user=1,
            timeout_seconds=0.02,
        )
        lease = await user_limiter.acquire(staff, first_session)
        try:
            await assert_limit(
                user_limiter,
                staff,
                second_session,
                "LIVE_TRANSCRIPT_USER_LIMIT",
            )
        finally:
            lease.release()

        session_limiter = LiveConnectionLimiter(
            db_engine=engine,
            max_global=2,
            max_clinic=2,
            max_user=2,
            timeout_seconds=0.02,
        )
        lease = await session_limiter.acquire(staff, first_session)
        try:
            await assert_limit(
                session_limiter,
                staff,
                first_session,
                "LIVE_TRANSCRIPT_SESSION_IN_USE",
            )
        finally:
            lease.release()

        # Releasing the connection-scoped advisory locks permits an explicit
        # reconnect for that same clinic, user, and session.
        reconnected = await session_limiter.acquire(staff, first_session)
        reconnected.release()

    asyncio.run(run())


def test_deterministic_live_is_never_used_for_ordinary_audio(
    client: TestClient, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_deterministic_live(monkeypatch)
    clinician = auth_headers("clinician")
    patient_id = _patient(client, clinician)
    created = client.post(
        "/api/v1/voice/sessions",
        headers=clinician,
        json={"patient_id": patient_id, "capture_kind": "clinical"},
    )
    session_id = created.json()["id"]
    joined = client.post(
        f"/api/v1/voice/sessions/{session_id}/devices",
        headers=clinician,
        json={
            "client_device_id": "ordinary-live-browser",
            "capture_role": "patient",
            "expected_patient_id": patient_id,
            "expected_capture_kind": "clinical",
        },
    )
    assert joined.status_code == 201
    status = client.get(f"/api/v1/voice/sessions/{session_id}/live", headers=clinician)
    assert status.json() == {
        "available": False,
        "status": "unavailable",
        "reason_code": "LIVE_TRANSCRIPT_FIXTURE_REQUIRED",
        "provider": None,
        "model": None,
        "provisional": True,
    }
