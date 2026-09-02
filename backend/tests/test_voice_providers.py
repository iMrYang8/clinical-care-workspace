import asyncio
import importlib
import json
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.services.voice import diarization
from app.services.voice.ffmpeg import (
    AudioPreprocessingError,
    DeviceAudio,
    _input_args,
    _ordered_device_payload,
    _pcm_signals,
    preprocess_audio,
)
from app.services.voice.providers import local_whisper_worker
from app.services.voice.providers.deterministic import SyntheticFixtureProvider
from app.services.voice.providers.local_whisper import LocalFasterWhisperProvider
from app.services.voice.providers.openai_audio import OpenAIAudioTranscriptionProvider


class _Response:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class _AsyncClient:
    payload: Any = {}
    request: dict[str, Any] = {}
    timeout: httpx.Timeout | None = None

    def __init__(self, *, timeout: httpx.Timeout) -> None:
        self.__class__.timeout = timeout

    async def __aenter__(self) -> "_AsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.__class__.request = {"url": url, **kwargs}
        return _Response(self.__class__.payload)


@pytest.mark.unit
def test_openai_audio_adapter_normalizes_timestamped_segments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"RIFF-local-fixture")
    _AsyncClient.payload = {
        "text": "Hello patient penicillin allergy",
        "language": "en",
        "segments": [
            {
                "text": "Hello patient",
                "start": 0.0,
                "end": 1.25,
                "speaker": "SPEAKER_00",
                "language": "en",
                "confidence": 0.93,
            },
            {
                "text": "penicillin allergy",
                "start": 1.0,
                "end": 2.5,
            },
            {"text": "   ", "start": 2.5, "end": 2.75},
            {"text": "invalid range", "start": 3.0, "end": 3.0},
            {"start": 3.0, "end": 4.0},
        ],
    }
    monkeypatch.setattr(
        "app.services.voice.providers.openai_audio.httpx.AsyncClient", _AsyncClient
    )
    provider = OpenAIAudioTranscriptionProvider(
        api_key="TOKEN",
        model="gpt-final-transcribe",
        timeout_seconds=9,
        connect_timeout_seconds=4,
    )

    result = asyncio.run(provider.transcribe(audio))

    assert result.provider == "openai"
    assert result.model == "gpt-final-transcribe"
    assert result.segments[0].start_ms == 0
    assert result.segments[0].end_ms == 1_250
    assert result.segments[0].confidence_source == "provider"
    assert result.segments[1].confidence_source == "unavailable"
    assert result.segments[0].overlap_group_id == "openai-overlap-0"
    assert result.segments[1].overlap_group_id == "openai-overlap-0"
    assert result.warnings == ("PROVIDER_DROPPED_INVALID_SEGMENT_RANGE",)
    assert _AsyncClient.request["data"]["response_format"] == "diarized_json"
    assert _AsyncClient.request["headers"] == {"Authorization": "Bearer TOKEN"}
    assert _AsyncClient.timeout is not None
    assert _AsyncClient.timeout.connect == 4
    assert _AsyncClient.timeout.read == 9
    assert _AsyncClient.timeout.write == 9
    assert _AsyncClient.timeout.pool == 9


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,error",
    [
        ({"segments": []}, "missing text"),
        ({"text": "hello", "segments": "bad"}, "no timestamped segments"),
    ],
)
def test_openai_audio_adapter_rejects_incomplete_provider_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: Any,
    error: str,
) -> None:
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"local")
    _AsyncClient.payload = payload
    monkeypatch.setattr(
        "app.services.voice.providers.openai_audio.httpx.AsyncClient", _AsyncClient
    )
    with pytest.raises(ValueError, match=error):
        asyncio.run(
            OpenAIAudioTranscriptionProvider(api_key="TOKEN", model="MODEL").transcribe(
                audio
            )
        )


@pytest.mark.unit
def test_local_asr_uses_cpu_int8_and_never_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = tmp_path / "cached-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    observed: dict[str, Any] = {}

    class FakeWhisperModel:
        def __init__(self, path: str, **kwargs: Any) -> None:
            observed.update({"path": path, **kwargs})

        def transcribe(self, path: str, *, vad_filter: bool):
            observed.update({"audio": path, "vad_filter": vad_filter})
            return (
                [
                    SimpleNamespace(text=" first ", start=0.0, end=0.5),
                    SimpleNamespace(text="", start=0.5, end=0.6),
                    SimpleNamespace(text="second", start=0.6, end=1.2),
                ],
                SimpleNamespace(language="en"),
            )

    monkeypatch.setattr(
        "app.services.voice.providers.local_whisper.importlib.import_module",
        lambda _name: SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"local-audio")

    result = LocalFasterWhisperProvider(str(model_dir))._transcribe_sync(audio)

    assert result.text == "first\nsecond"
    assert observed["device"] == "cpu"
    assert observed["compute_type"] == "int8"
    assert observed["local_files_only"] is True
    assert observed["vad_filter"] is True
    assert result.warnings == ("LOCAL_ASR_NO_DIARIZATION",)


@pytest.mark.unit
def test_local_asr_requires_cached_model_and_optional_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="non-empty local directory"):
        LocalFasterWhisperProvider(str(tmp_path / "missing"))
    model_dir = tmp_path / "cached-model"
    model_dir.mkdir()
    with pytest.raises(ValueError, match="non-empty local directory"):
        LocalFasterWhisperProvider(str(model_dir))
    (model_dir / "config.json").write_text("{}")
    provider = LocalFasterWhisperProvider(str(model_dir))

    def missing_dependency(_name: str) -> Any:
        raise ImportError("not installed")

    monkeypatch.setattr(importlib, "import_module", missing_dependency)
    with pytest.raises(RuntimeError, match="LOCAL_ASR_DEPENDENCY_UNAVAILABLE"):
        provider._transcribe_sync(tmp_path / "audio.wav")


@pytest.mark.unit
def test_local_asr_timeout_terminates_inference_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = tmp_path / "cached-model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    class HangingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.killed = False
            self.released = asyncio.Event()

        async def communicate(self) -> tuple[bytes, bytes]:
            await self.released.wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.released.set()

        async def wait(self) -> int:
            await self.released.wait()
            return self.returncode or 0

    process = HangingProcess()

    async def create_process(*_args: object, **_kwargs: object) -> HangingProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    provider = LocalFasterWhisperProvider(str(model_dir), timeout_seconds=0.01)
    with pytest.raises(TimeoutError):
        asyncio.run(provider.transcribe(audio))
    assert process.killed is True


@pytest.mark.unit
def test_local_asr_child_writes_only_normalized_private_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    result_path = tmp_path / "result.json"
    result = SyntheticFixtureProvider().transcribe_fixture("code-switch-overlap-v1")
    monkeypatch.setattr(
        local_whisper_worker, "transcribe_local_sync", lambda *_args: result
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local-whisper-worker",
            "--model-dir",
            str(model_dir),
            "--audio-path",
            str(audio),
            "--result-path",
            str(result_path),
        ],
    )

    assert local_whisper_worker.main() == 0
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["provider"] == "deterministic-synthetic-fixture"
    assert payload["segments"][1]["overlap_group_id"] == "overlap-1"
    assert result_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.unit
def test_local_asr_child_suppresses_internal_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(*_args: object) -> None:
        raise RuntimeError("sensitive model failure")

    result_path = tmp_path / "result.json"
    monkeypatch.setattr(local_whisper_worker, "transcribe_local_sync", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local-whisper-worker",
            "--model-dir",
            str(tmp_path),
            "--audio-path",
            str(tmp_path / "audio.wav"),
            "--result-path",
            str(result_path),
        ],
    )

    assert local_whisper_worker.main() == 2
    assert result_path.exists() is False


@pytest.mark.unit
def test_pyannote_readiness_is_gated_and_local_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "PYANNOTE_ENABLED", False)
    assert diarization.pyannote_runtime_status() == (False, "PYANNOTE_DISABLED")
    monkeypatch.setattr(settings, "PYANNOTE_ENABLED", True)
    monkeypatch.setattr(settings, "PYANNOTE_MODEL_DIR", None)
    assert diarization.pyannote_runtime_status() == (
        False,
        "PYANNOTE_LOCAL_MODEL_REQUIRED",
    )
    monkeypatch.setattr(settings, "PYANNOTE_MODEL_DIR", str(tmp_path / "missing"))
    assert diarization.pyannote_runtime_status() == (
        False,
        "PYANNOTE_MODEL_NOT_CACHED",
    )
    model_dir = tmp_path / "cached"
    model_dir.mkdir()
    monkeypatch.setattr(settings, "PYANNOTE_MODEL_DIR", str(model_dir))
    monkeypatch.setattr(diarization.importlib, "import_module", lambda _name: object())
    assert diarization.pyannote_runtime_status() == (
        False,
        "PYANNOTE_MODEL_NOT_CACHED",
    )
    (model_dir / "config.yaml").write_text("pipeline: synthetic-test\n")
    assert diarization.pyannote_runtime_status() == (
        True,
        "PYANNOTE_LOCAL_MODEL_READY",
    )

    def missing_dependency(_name: str) -> Any:
        raise ImportError("not installed")

    monkeypatch.setattr(diarization.importlib, "import_module", missing_dependency)
    assert diarization.pyannote_runtime_status() == (
        False,
        "PYANNOTE_DEPENDENCY_UNAVAILABLE",
    )


def _write_wav(path: Path, samples: list[int], *, sample_rate: int = 16_000) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


@pytest.mark.unit
def test_ffmpeg_input_validation_and_pcm_review_signals(tmp_path: Path) -> None:
    payload = b"chunk-0"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    device = DeviceAudio("device", "audio/webm;codecs=opus", [(0, payload, digest)])
    assert _ordered_device_payload(device) == payload
    assert _input_args("audio/webm;codecs=opus", tmp_path / "input") == [
        "-protocol_whitelist",
        "file",
        "-f",
        "matroska,webm",
        "-i",
        str(tmp_path / "input"),
    ]

    with pytest.raises(AudioPreprocessingError, match="AUDIO_CHUNK_SEQUENCE_INVALID"):
        _ordered_device_payload(
            DeviceAudio("device", "audio/webm", [(1, payload, digest)])
        )
    with pytest.raises(AudioPreprocessingError, match="AUDIO_CHUNK_HASH_INVALID"):
        _ordered_device_payload(
            DeviceAudio("device", "audio/webm", [(0, payload, "0" * 64)])
        )

    output = tmp_path / "signals.wav"
    _write_wav(output, [0, 0, 32_767, 100])
    duration, signals = _pcm_signals(output, multi_device=True)
    assert duration == 0
    assert signals["clipping_review"] is True
    assert signals["overlap_review"] is True
    assert signals["alignment"] == "track-start-only"

    invalid = tmp_path / "invalid.wav"
    _write_wav(invalid, [0, 1], sample_rate=8_000)
    with pytest.raises(AudioPreprocessingError, match="FFMPEG_OUTPUT_FORMAT_INVALID"):
        _pcm_signals(invalid, multi_device=False)

    over_limit = tmp_path / "over-limit.wav"
    _write_wav(over_limit, [0] * 32_001)
    with pytest.raises(AudioPreprocessingError, match="AUDIO_DECODE_LIMIT_EXCEEDED"):
        _pcm_signals(over_limit, multi_device=False, max_duration_ms=2_000)


@pytest.mark.unit
def test_ffmpeg_rejects_disguised_network_playlist() -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg binary is not installed")
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\nhttp://127.0.0.1:9/private\n"
    device = DeviceAudio(
        "device",
        "audio/webm",
        [(0, playlist, __import__("hashlib").sha256(playlist).hexdigest())],
    )
    with pytest.raises(AudioPreprocessingError, match="FFMPEG_PREPROCESSING_FAILED"):
        preprocess_audio([device], ffmpeg_bin=ffmpeg, timeout_seconds=2)


@pytest.mark.unit
def test_ffmpeg_failure_modes_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AudioPreprocessingError, match="NO_AUDIO_CHUNKS"):
        preprocess_audio([], ffmpeg_bin="FFMPEG", timeout_seconds=1)

    payload = b"not-real-audio"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    unknown_device = DeviceAudio("device", "audio/unknown", [(0, payload, digest)])

    with pytest.raises(
        AudioPreprocessingError, match="AUDIO_LIMIT_CONFIGURATION_INVALID"
    ):
        preprocess_audio(
            [unknown_device],
            ffmpeg_bin="FFMPEG",
            timeout_seconds=1,
            max_duration_ms=2_000,
            max_output_bytes=64_000,
        )
    with pytest.raises(AudioPreprocessingError, match="AUDIO_MEDIA_TYPE_INVALID"):
        preprocess_audio([unknown_device], ffmpeg_bin="FFMPEG", timeout_seconds=1)

    device = DeviceAudio("device", "audio/webm", [(0, payload, digest)])

    def timeout(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("FFMPEG", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AudioPreprocessingError, match="FFMPEG_TIMEOUT"):
        preprocess_audio([device], ffmpeg_bin="FFMPEG", timeout_seconds=0)

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("missing")

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(AudioPreprocessingError, match="FFMPEG_UNAVAILABLE"):
        preprocess_audio([device], ffmpeg_bin="FFMPEG", timeout_seconds=1)

    observed_run: dict[str, Any] = {}

    def failed_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        observed_run.update({"args": args, **kwargs})
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(AudioPreprocessingError, match="FFMPEG_PREPROCESSING_FAILED"):
        preprocess_audio([device], ffmpeg_bin="FFMPEG", timeout_seconds=1)
    assert observed_run["stdout"] is subprocess.DEVNULL
    assert observed_run["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in observed_run
    command = observed_run["args"][0]
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    assert command[command.index("-f") + 1] == "matroska,webm"
