from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from starlette.websockets import WebSocketState

from app.api.routes import voice_live
from app.core.config import settings
from app.models import ProviderCircuitState, VoiceSession, get_datetime_utc
from app.services.provider_resilience import ProviderCircuitOpen, ProviderFailure
from app.services.voice import diarization, pyannote_worker, worker
from app.services.voice.diarization import DiarizationTurn, LocalPyannoteDiarizer
from app.services.voice.live_providers import LiveTranscriptionError
from app.services.voice.providers.base import TranscriptResult, TranscriptSegmentResult

pytestmark = pytest.mark.unit


class _CompletedProcess:
    def __init__(self, returncode: int | None = 0) -> None:
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def _cached_model(tmp_path: Path) -> Path:
    model_dir = tmp_path / "cached-pyannote"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("offline: true\n", encoding="utf-8")
    return model_dir


def test_local_pyannote_rejects_empty_model_directory(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="non-empty local directory"):
        LocalPyannoteDiarizer(str(empty))


def test_local_pyannote_subprocess_is_offline_and_parses_bounded_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _cached_model(tmp_path)
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF synthetic")
    captured: dict[str, Any] = {}

    async def spawn(*args: str, **kwargs: Any) -> _CompletedProcess:
        captured["args"] = args
        captured["env"] = kwargs["env"]
        result_path = Path(args[args.index("--result-path") + 1])
        result_path.write_text(
            json.dumps(
                [
                    {
                        "start_ms": 0,
                        "end_ms": 800,
                        "speaker_id": "SPEAKER_00",
                        "confidence": 0.91,
                    },
                    "ignored-non-object",
                    {
                        "start_ms": 700,
                        "end_ms": 1_200,
                        "speaker_id": "SPEAKER_01",
                        "confidence": None,
                    },
                ]
            ),
            encoding="utf-8",
        )
        return _CompletedProcess()

    monkeypatch.setenv("HF_TOKEN", "must-not-reach-child")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "must-not-reach-child")
    monkeypatch.setattr(diarization.asyncio, "create_subprocess_exec", spawn)

    turns = asyncio.run(
        LocalPyannoteDiarizer(str(model_dir), timeout_seconds=1).diarize(audio_path)
    )

    assert turns == [
        DiarizationTurn(0, 800, "SPEAKER_00", 0.91),
        DiarizationTurn(700, 1_200, "SPEAKER_01", None),
    ]
    child_env = captured["env"]
    assert child_env["HF_HUB_OFFLINE"] == "1"
    assert child_env["TRANSFORMERS_OFFLINE"] == "1"
    assert child_env["HF_DATASETS_OFFLINE"] == "1"
    assert "HF_TOKEN" not in child_env
    assert "HUGGING_FACE_HUB_TOKEN" not in child_env
    assert "--audio-path" in captured["args"]


@pytest.mark.parametrize(
    ("payload", "returncode", "expected_code"),
    [
        (None, 2, "LOCAL_DIARIZATION_PROCESS_FAILED"),
        (b"{}", 0, "LOCAL_DIARIZATION_RESULT_INVALID"),
        (
            b'[{"start_ms":-1,"end_ms":20,"speaker_id":"SPEAKER_00"}]',
            0,
            "LOCAL_DIARIZATION_RESULT_INVALID",
        ),
        (
            b'[{"start_ms":0,"end_ms":20,"speaker_id":""}]',
            0,
            "LOCAL_DIARIZATION_RESULT_INVALID",
        ),
    ],
)
def test_local_pyannote_fails_closed_on_process_and_schema_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes | None,
    returncode: int,
    expected_code: str,
) -> None:
    model_dir = _cached_model(tmp_path)

    async def spawn(*args: str, **_kwargs: Any) -> _CompletedProcess:
        if payload is not None:
            result_path = Path(args[args.index("--result-path") + 1])
            result_path.write_bytes(payload)
        return _CompletedProcess(returncode)

    monkeypatch.setattr(diarization.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match=expected_code):
        asyncio.run(LocalPyannoteDiarizer(str(model_dir)).diarize(tmp_path / "a.wav"))


def test_local_pyannote_rejects_oversized_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _cached_model(tmp_path)

    async def spawn(*args: str, **_kwargs: Any) -> _CompletedProcess:
        result_path = Path(args[args.index("--result-path") + 1])
        result_path.write_bytes(b"[" + b" " * (4 * 1024 * 1024) + b"]")
        return _CompletedProcess()

    monkeypatch.setattr(diarization.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="LOCAL_DIARIZATION_RESULT_TOO_LARGE"):
        asyncio.run(LocalPyannoteDiarizer(str(model_dir)).diarize(tmp_path / "a.wav"))


def test_local_pyannote_spawn_error_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _cached_model(tmp_path)

    async def unavailable(*_args: str, **_kwargs: Any) -> _CompletedProcess:
        raise OSError("synthetic exec denial")

    monkeypatch.setattr(diarization.asyncio, "create_subprocess_exec", unavailable)
    with pytest.raises(RuntimeError, match="LOCAL_DIARIZATION_PROCESS_UNAVAILABLE"):
        asyncio.run(LocalPyannoteDiarizer(str(model_dir)).diarize(tmp_path / "a.wav"))


def test_local_pyannote_timeout_kills_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _cached_model(tmp_path)

    class HangingProcess(_CompletedProcess):
        def __init__(self) -> None:
            super().__init__(None)

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    process = HangingProcess()

    async def spawn(*_args: str, **_kwargs: Any) -> HangingProcess:
        return process

    monkeypatch.setattr(diarization.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        asyncio.run(
            LocalPyannoteDiarizer(str(model_dir), timeout_seconds=0.01).diarize(
                tmp_path / "a.wav"
            )
        )
    assert process.killed is True


def test_local_pyannote_cancellation_kills_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = _cached_model(tmp_path)
    communicating = asyncio.Event()

    class HangingProcess(_CompletedProcess):
        def __init__(self) -> None:
            super().__init__(None)

        async def communicate(self) -> tuple[bytes, bytes]:
            communicating.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    process = HangingProcess()

    async def spawn(*_args: str, **_kwargs: Any) -> HangingProcess:
        return process

    async def exercise() -> None:
        task = asyncio.create_task(
            LocalPyannoteDiarizer(str(model_dir), timeout_seconds=30).diarize(
                tmp_path / "a.wav"
            )
        )
        await communicating.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(diarization.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(exercise())
    assert process.killed is True


def test_terminate_process_suppresses_lookup_and_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class GoneProcess(_CompletedProcess):
        def __init__(self) -> None:
            super().__init__(None)

        def kill(self) -> None:
            raise ProcessLookupError

    async def timeout(awaitable: Any, *, timeout: float) -> None:
        del timeout
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(diarization.asyncio, "wait_for", timeout)
    asyncio.run(diarization._terminate_process(cast(Any, GoneProcess())))


def _segment(*, start_ms: int = 0, end_ms: int = 1_000) -> TranscriptSegmentResult:
    return TranscriptSegmentResult(
        text="penicillin allergy",
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id=None,
        detected_language="en",
        confidence=0.9,
        confidence_source="provider",
        overlap_group_id=None,
        text_start=0,
        text_end=18,
        source_language="en",
        language_confidence=0.95,
    )


def _transcript(*, provider: str = "fixture-local") -> TranscriptResult:
    return TranscriptResult(
        text="penicillin allergy",
        segments=[_segment()],
        provider=provider,
        model="offline-fixture-v1",
        detected_language="en",
        warnings=("LOCAL_ASR_NO_DIARIZATION",),
    )


def test_diarization_partial_alignment_and_result_warning_replacement() -> None:
    no_match = _segment(start_ms=0, end_ms=1_000)
    weak_match = _segment(start_ms=1_000, end_ms=2_000)
    result = TranscriptResult(
        text="penicillin allergy",
        segments=[no_match, weak_match],
        provider="fixture-local",
        model="offline-fixture-v1",
        warnings=("LOCAL_ASR_NO_DIARIZATION", "UPSTREAM_REVIEW"),
    )

    aligned = diarization.apply_local_diarization(
        result,
        [DiarizationTurn(1_100, 1_200, "SPEAKER_02")],
    )

    assert aligned.segments[0] is no_match
    assert aligned.segments[1].speaker_id == "SPEAKER_02"
    assert aligned.segments[1].speaker_ids == ("SPEAKER_02",)
    assert aligned.warnings == ("LOCAL_DIARIZATION_PARTIAL", "UPSTREAM_REVIEW")


def test_pyannote_worker_serializes_pipeline_turns_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, str] = {}

    class Turn:
        def __init__(self, start: float, end: float) -> None:
            self.start = start
            self.end = end

    class Annotation:
        def itertracks(self, *, yield_label: bool):
            assert yield_label is True
            yield Turn(0.125, 0.875), "track-1", "SPEAKER_00"
            yield Turn(0.875, 1.5), "track-2", "SPEAKER_01"

    class PipelineOutput:
        speaker_diarization = Annotation()

    class Pipeline:
        @classmethod
        def from_pretrained(cls, model_dir: str) -> Pipeline:
            calls["model_dir"] = model_dir
            return cls()

        def __call__(self, audio_path: str) -> PipelineOutput:
            calls["audio_path"] = audio_path
            return PipelineOutput()

    model_dir = tmp_path / "model"
    audio_path = tmp_path / "audio.wav"
    result_path = tmp_path / "turns.json"
    model_dir.mkdir()
    audio_path.write_bytes(b"RIFF fixture")
    monkeypatch.setattr(
        pyannote_worker.importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(Pipeline=Pipeline) if name == "pyannote.audio" else None
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pyannote-worker",
            "--model-dir",
            str(model_dir),
            "--audio-path",
            str(audio_path),
            "--result-path",
            str(result_path),
        ],
    )

    pyannote_worker.main()

    assert calls == {
        "model_dir": str(model_dir.resolve()),
        "audio_path": str(audio_path.resolve()),
    }
    assert json.loads(result_path.read_text(encoding="utf-8")) == [
        {
            "start_ms": 125,
            "end_ms": 875,
            "speaker_id": "SPEAKER_00",
            "confidence": None,
        },
        {
            "start_ms": 875,
            "end_ms": 1500,
            "speaker_id": "SPEAKER_01",
            "confidence": None,
        },
    ]
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert os.environ["HF_DATASETS_OFFLINE"] == "1"


def test_audio_provider_selection_fails_closed_before_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_session = SimpleNamespace(synthetic_fixture=True, clinic_id=uuid.uuid4())
    assert worker._configured_provider(
        cast(Session, object()), cast(Any, voice_session)
    ) == (
        None,
        None,
    )

    voice_session.synthetic_fixture = False
    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "disabled")
    assert worker._configured_provider(
        cast(Session, object()), cast(Any, voice_session)
    ) == (
        None,
        "ASR_PROVIDER_DISABLED",
    )

    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "openai")
    monkeypatch.setattr(settings, "STRICT_NO_AUDIO_EGRESS", True)
    assert worker._configured_provider(
        cast(Session, object()), cast(Any, voice_session)
    ) == (
        None,
        "STRICT_NO_AUDIO_EGRESS",
    )

    monkeypatch.setattr(settings, "STRICT_NO_AUDIO_EGRESS", False)
    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", False)
    assert worker._configured_provider(
        cast(Session, object()), cast(Any, voice_session)
    ) == (
        None,
        "REMOTE_AUDIO_EGRESS_DISABLED",
    )

    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", True)
    monkeypatch.setattr(
        worker,
        "remote_audio_egress_denial",
        lambda *_args: "CLINIC_REMOTE_AUDIO_EGRESS_DISABLED",
    )
    assert worker._configured_provider(
        cast(Session, object()), cast(Any, voice_session)
    ) == (
        None,
        "CLINIC_REMOTE_AUDIO_EGRESS_DISABLED",
    )


def test_audio_provider_selection_constructs_only_qualified_local_or_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_session = SimpleNamespace(synthetic_fixture=False, clinic_id=uuid.uuid4())
    db = cast(Session, object())
    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "openai")
    monkeypatch.setattr(settings, "STRICT_NO_AUDIO_EGRESS", False)
    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", True)
    monkeypatch.setattr(worker, "remote_audio_egress_denial", lambda *_args: None)
    monkeypatch.setattr(
        worker,
        "clinic_ai_runtime",
        lambda *_args: SimpleNamespace(api_key=None, transcribe_model=None),
    )
    assert worker._configured_provider(db, cast(Any, voice_session)) == (
        None,
        "OPENAI_AUDIO_NOT_CONFIGURED",
    )

    captured: dict[str, Any] = {}

    class RemoteProvider:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        worker,
        "clinic_ai_runtime",
        lambda *_args: SimpleNamespace(
            api_key="fixture-key", transcribe_model="fixture-model"
        ),
    )
    monkeypatch.setattr(worker, "OpenAIAudioTranscriptionProvider", RemoteProvider)
    remote, reason = worker._configured_provider(db, cast(Any, voice_session))
    assert isinstance(remote, RemoteProvider)
    assert reason is None
    assert captured["model"] == "fixture-model"
    assert captured["timeout_seconds"] == settings.REMOTE_REQUEST_TIMEOUT_SECONDS
    assert (
        captured["connect_timeout_seconds"] == settings.REMOTE_CONNECT_TIMEOUT_SECONDS
    )

    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "local")
    monkeypatch.setattr(settings, "LOCAL_ASR_MODEL_DIR", None)
    assert worker._configured_provider(db, cast(Any, voice_session)) == (
        None,
        "LOCAL_ASR_MODEL_REQUIRED",
    )

    monkeypatch.setattr(settings, "LOCAL_ASR_MODEL_DIR", "/fixture/model")

    class MissingLocal:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise ValueError("not cached")

    monkeypatch.setattr(worker, "LocalFasterWhisperProvider", MissingLocal)
    assert worker._configured_provider(db, cast(Any, voice_session)) == (
        None,
        "LOCAL_ASR_MODEL_NOT_CACHED",
    )

    local_provider = object()
    monkeypatch.setattr(
        worker,
        "LocalFasterWhisperProvider",
        lambda *_args, **_kwargs: local_provider,
    )
    assert worker._configured_provider(db, cast(Any, voice_session)) == (
        local_provider,
        None,
    )

    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "unknown")
    assert worker._configured_provider(db, cast(Any, voice_session)) == (
        None,
        "ASR_PROVIDER_UNAVAILABLE",
    )


class _FixtureProvider:
    def __init__(
        self,
        result: TranscriptResult | None = None,
        *,
        provider_name: str = "fixture-local",
        error: BaseException | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.result = result or _transcript(provider=provider_name)
        self.error = error

    async def transcribe(self, _audio_path: Path) -> TranscriptResult:
        if self.error is not None:
            raise self.error
        return self.result


def _voice_session(**overrides: Any) -> VoiceSession:
    values: dict[str, Any] = {
        "clinic_id": uuid.uuid4(),
        "patient_id": uuid.uuid4(),
        "capture_kind": "clinical",
        "created_by_id": uuid.uuid4(),
        "synthetic_fixture": False,
    }
    values.update(overrides)
    return VoiceSession(**values)


def test_transcribe_handles_missing_fixture_disabled_provider_and_open_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_fixture = _voice_session(synthetic_fixture=True, fixture_id=None)
    assert asyncio.run(
        worker._transcribe(cast(Session, object()), missing_fixture, b"x")
    ) == (
        None,
        "SYNTHETIC_FIXTURE_ID_MISSING",
        None,
    )

    regular = _voice_session()
    monkeypatch.setattr(
        worker,
        "_configured_provider",
        lambda *_args: (None, "ASR_PROVIDER_DISABLED"),
    )
    assert asyncio.run(worker._transcribe(cast(Session, object()), regular, b"x")) == (
        None,
        "ASR_PROVIDER_DISABLED",
        None,
    )

    remote = _FixtureProvider(provider_name="openai")
    monkeypatch.setattr(worker, "_configured_provider", lambda *_args: (remote, None))
    monkeypatch.setattr(
        worker,
        "_assert_audio_circuit_available",
        lambda *_args: (_ for _ in ()).throw(
            ProviderCircuitOpen("PROVIDER_CIRCUIT_OPEN")
        ),
    )
    result, code, failure = asyncio.run(
        worker._transcribe(cast(Session, object()), regular, b"RIFF")
    )
    assert result is None
    assert code == "PROVIDER_CIRCUIT_OPEN"
    assert failure == ProviderFailure("PROVIDER_CIRCUIT_OPEN", "transient", True)


def test_local_transcription_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FixtureProvider(error=TimeoutError())
    monkeypatch.setattr(worker, "_configured_provider", lambda *_args: (provider, None))
    result, code, failure = asyncio.run(
        worker._transcribe(cast(Session, object()), _voice_session(), b"RIFF")
    )
    assert (result, code, failure) == (None, "ASR_TIMEOUT", None)


def test_local_transcription_marks_uncached_diarization_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PYANNOTE_ENABLED", True)
    monkeypatch.setattr(
        worker,
        "_configured_provider",
        lambda *_args: (_FixtureProvider(), None),
    )
    monkeypatch.setattr(
        worker,
        "pyannote_runtime_status",
        lambda: (False, "PYANNOTE_MODEL_NOT_CACHED"),
    )

    result, code, failure = asyncio.run(
        worker._transcribe(cast(Session, object()), _voice_session(), b"RIFF")
    )

    assert code is None and failure is None and result is not None
    assert result.warnings == (
        "LOCAL_ASR_NO_DIARIZATION",
        "LOCAL_DIARIZATION_UNAVAILABLE",
        "PYANNOTE_MODEL_NOT_CACHED",
    )


def test_local_transcription_applies_cached_diarization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "PYANNOTE_ENABLED", True)
    monkeypatch.setattr(settings, "PYANNOTE_MODEL_DIR", str(_cached_model(tmp_path)))
    monkeypatch.setattr(
        worker,
        "_configured_provider",
        lambda *_args: (_FixtureProvider(), None),
    )
    monkeypatch.setattr(worker, "pyannote_runtime_status", lambda: (True, "READY"))

    class FakeDiarizer:
        def __init__(self, model_dir: str, *, timeout_seconds: float) -> None:
            assert model_dir == settings.PYANNOTE_MODEL_DIR
            assert 1 <= timeout_seconds <= 300

        async def diarize(self, _path: Path) -> list[DiarizationTurn]:
            return [DiarizationTurn(0, 1_000, "SPEAKER_00")]

    monkeypatch.setattr(worker, "LocalPyannoteDiarizer", FakeDiarizer)
    result, code, failure = asyncio.run(
        worker._transcribe(cast(Session, object()), _voice_session(), b"RIFF")
    )

    assert code is None and failure is None and result is not None
    assert result.segments[0].speaker_id == "SPEAKER_00"
    assert result.segments[0].speaker_ids == ("SPEAKER_00",)
    assert "LOCAL_ASR_NO_DIARIZATION" not in result.warnings


@pytest.mark.parametrize(
    ("error", "expected_warning"),
    [
        (TimeoutError(), "LOCAL_DIARIZATION_TIMEOUT"),
        (RuntimeError("invalid local result"), None),
        (ValueError("invalid local model"), None),
    ],
)
def test_local_transcription_diarization_failure_never_blocks_manual_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException,
    expected_warning: str | None,
) -> None:
    monkeypatch.setattr(settings, "PYANNOTE_ENABLED", True)
    monkeypatch.setattr(settings, "PYANNOTE_MODEL_DIR", str(_cached_model(tmp_path)))
    monkeypatch.setattr(
        worker,
        "_configured_provider",
        lambda *_args: (_FixtureProvider(), None),
    )
    monkeypatch.setattr(worker, "pyannote_runtime_status", lambda: (True, "READY"))

    class FailingDiarizer:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def diarize(self, _path: Path) -> list[DiarizationTurn]:
            raise error

    monkeypatch.setattr(worker, "LocalPyannoteDiarizer", FailingDiarizer)
    result, code, failure = asyncio.run(
        worker._transcribe(cast(Session, object()), _voice_session(), b"RIFF")
    )

    assert code is None and failure is None and result is not None
    assert "LOCAL_DIARIZATION_UNAVAILABLE" in result.warnings
    if expected_warning is not None:
        assert expected_warning in result.warnings


class _FirstResult:
    def __init__(self, value: ProviderCircuitState | None) -> None:
        self.value = value

    def first(self) -> ProviderCircuitState | None:
        return self.value


class _CircuitSession:
    def __init__(self, circuit: ProviderCircuitState | None) -> None:
        self.circuit = circuit
        self.added: list[object] = []
        self.flush_count = 0

    def exec(self, _statement: object) -> _FirstResult:
        return _FirstResult(self.circuit)

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        self.flush_count += 1


def _circuit(*, state: str = "open") -> ProviderCircuitState:
    return ProviderCircuitState(
        clinic_id=uuid.uuid4(),
        provider="openai",
        capability="audio_transcription",
        state=state,
        consecutive_failures=3,
        last_error_class="timeout",
        opened_at=get_datetime_utc(),
    )


def test_audio_circuit_blocks_early_probe_then_half_opens_and_recovers() -> None:
    clinic_id = uuid.uuid4()
    empty = _CircuitSession(None)
    worker._assert_audio_circuit_available(cast(Session, empty), clinic_id)

    closed = _CircuitSession(_circuit(state="closed"))
    worker._assert_audio_circuit_available(cast(Session, closed), clinic_id)

    circuit = _circuit()
    circuit.next_probe_at = get_datetime_utc().replace(microsecond=0)
    due = _CircuitSession(circuit)
    worker._assert_audio_circuit_available(cast(Session, due), clinic_id)
    assert circuit.state == "half_open"
    assert due.flush_count == 1

    circuit.state = "open"
    circuit.next_probe_at = get_datetime_utc() + __import__("datetime").timedelta(
        seconds=60
    )
    with pytest.raises(ProviderCircuitOpen, match="PROVIDER_CIRCUIT_OPEN"):
        worker._assert_audio_circuit_available(cast(Session, due), clinic_id)

    empty_job = SimpleNamespace(clinic_id=clinic_id)
    worker._record_audio_provider_success(cast(Session, empty), cast(Any, empty_job))
    job = SimpleNamespace(clinic_id=clinic_id)
    worker._record_audio_provider_success(cast(Session, due), cast(Any, job))
    assert circuit.state == "closed"
    assert circuit.consecutive_failures == 0
    assert circuit.last_error_class is None
    assert circuit.opened_at is None
    assert circuit.next_probe_at is None
    assert circuit.last_success_at is not None


def test_audio_circuit_unique_race_reloads_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = _circuit(state="closed")
    responses = iter([None, winner])
    monkeypatch.setattr(
        worker, "_audio_circuit", lambda *_args, **_kwargs: next(responses)
    )

    class RacingSession:
        def __init__(self) -> None:
            self.flushes = 0

        def begin_nested(self):
            return nullcontext()

        def add(self, _value: object) -> None:
            return None

        def flush(self) -> None:
            self.flushes += 1
            raise IntegrityError("insert", {}, RuntimeError("synthetic race"))

    job = SimpleNamespace(id=uuid.uuid4(), clinic_id=winner.clinic_id, max_attempts=6)
    retry_at, selected = worker._record_audio_provider_failure(
        cast(Session, RacingSession()),
        cast(Any, job),
        ProviderFailure("PROVIDER_TIMEOUT", "timeout", True),
        attempt_no=1,
    )
    assert selected is winner
    assert retry_at is not None
    assert winner.state == "open"
    assert winner.next_probe_at == retry_at


def test_audio_circuit_unique_race_without_winner_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "_audio_circuit", lambda *_args, **_kwargs: None)

    class RacingSession:
        def begin_nested(self):
            return nullcontext()

        def add(self, _value: object) -> None:
            return None

        def flush(self) -> None:
            raise IntegrityError("insert", {}, RuntimeError("synthetic race"))

    job = SimpleNamespace(id=uuid.uuid4(), clinic_id=uuid.uuid4(), max_attempts=6)
    with pytest.raises(IntegrityError):
        worker._record_audio_provider_failure(
            cast(Session, RacingSession()),
            cast(Any, job),
            ProviderFailure("PROVIDER_TIMEOUT", "timeout", True),
            attempt_no=1,
        )


class _HelperWebSocket:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        send_timeout: bool = False,
    ) -> None:
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.application_state = WebSocketState.CONNECTED
        self.send_timeout = send_timeout
        self.sent: list[dict[str, object]] = []
        self.closed: list[int] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        if self.send_timeout:
            raise TimeoutError
        self.sent.append(payload)

    async def close(self, *, code: int) -> None:
        self.closed.append(code)
        self.application_state = WebSocketState.DISCONNECTED


def test_live_socket_helpers_enforce_origin_credentials_and_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_origin = _HelperWebSocket()
    assert voice_live._trusted_origin(cast(Any, no_origin)) is False

    monkeypatch.setattr(
        settings,
        "BROWSER_TRUSTED_ORIGINS",
        "https://secondary.example, https://third.example/path",
    )
    trusted = _HelperWebSocket(headers={"origin": "https://third.example/ignored"})
    assert voice_live._trusted_origin(cast(Any, trusted)) is True
    assert voice_live._credential(cast(Any, trusted)) is None

    bearer = _HelperWebSocket(headers={"authorization": "Bearer fixture-token"})
    assert voice_live._credential(cast(Any, bearer)) == "fixture-token"
    cookie = _HelperWebSocket(cookies={settings.AUTH_COOKIE_NAME: "cookie-token"})
    assert voice_live._credential(cast(Any, cookie)) == "cookie-token"

    asyncio.run(voice_live._safe_send(cast(Any, trusted), {"status": "available"}))
    assert trusted.sent == [{"status": "available"}]

    blocked = _HelperWebSocket(send_timeout=True)
    with pytest.raises(
        LiveTranscriptionError, match="LIVE_TRANSCRIPT_CLIENT_BACKPRESSURE"
    ):
        asyncio.run(voice_live._safe_send(cast(Any, blocked), {"status": "x"}))
    asyncio.run(voice_live._close(cast(Any, blocked), 1011))
    assert blocked.closed == [1011]
