from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
    validate_transcript_result,
)


class LocalFasterWhisperProvider:
    provider_name = "faster-whisper-local"

    def __init__(self, model_dir: str, *, timeout_seconds: float = 600) -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        if not self.model_dir.is_dir() or not any(self.model_dir.iterdir()):
            raise ValueError("LOCAL_ASR_MODEL_DIR must be a non-empty local directory")
        self.timeout_seconds = max(0.01, timeout_seconds)

    async def transcribe(self, audio_path: Path) -> TranscriptResult:
        # CTranslate2 inference cannot be cancelled safely inside a Python
        # thread. Run it in a dedicated process so timeout/cancellation can
        # terminate the actual CPU work before a retry is accepted.
        with tempfile.TemporaryDirectory(prefix="nightingale-local-asr-") as temp_name:
            temp_dir = Path(temp_name)
            os.chmod(temp_dir, stat.S_IRWXU)
            result_path = temp_dir / "result.json"
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.services.voice.providers.local_whisper_worker",
                    "--model-dir",
                    str(self.model_dir),
                    "--audio-path",
                    str(audio_path),
                    "--result-path",
                    str(result_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                raise RuntimeError("LOCAL_ASR_PROCESS_UNAVAILABLE") from exc
            try:
                await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout_seconds
                )
            except TimeoutError:
                await asyncio.shield(_terminate_process(process))
                raise
            except asyncio.CancelledError:
                await asyncio.shield(_terminate_process(process))
                raise
            if process.returncode != 0 or not result_path.is_file():
                raise RuntimeError("LOCAL_ASR_PROCESS_FAILED")
            if result_path.stat().st_size > 16 * 1024 * 1024:
                raise RuntimeError("LOCAL_ASR_RESULT_TOO_LARGE")
            try:
                payload: Any = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError
                raw_segments = payload.get("segments")
                if not isinstance(raw_segments, list):
                    raise ValueError
                result = TranscriptResult(
                    text=str(payload["text"]),
                    segments=[
                        TranscriptSegmentResult(**segment)
                        for segment in raw_segments
                        if isinstance(segment, dict)
                    ],
                    provider=str(payload["provider"]),
                    model=str(payload["model"]),
                    detected_language=(
                        str(payload["detected_language"])
                        if payload.get("detected_language") is not None
                        else None
                    ),
                    warnings=tuple(str(item) for item in payload.get("warnings", [])),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("LOCAL_ASR_RESULT_INVALID") from exc
            return validate_transcript_result(result)

    def _transcribe_sync(self, audio_path: Path) -> TranscriptResult:
        """Direct helper retained for deterministic adapter contract tests."""

        return transcribe_local_sync(self.model_dir, audio_path)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        # ``kill`` is the strongest portable subprocess signal. Do not leave
        # worker cleanup waiting indefinitely for a broken process wait.
        pass


def transcribe_local_sync(model_dir: Path, audio_path: Path) -> TranscriptResult:
    try:
        whisper_module = importlib.import_module("faster_whisper")
    except ImportError as exc:
        raise RuntimeError("LOCAL_ASR_DEPENDENCY_UNAVAILABLE") from exc
    whisper_model: Any = whisper_module.WhisperModel
    # A concrete local path and local_files_only prevent runtime downloads.
    model: Any = whisper_model(
        str(model_dir),
        device="cpu",
        compute_type="int8",
        local_files_only=True,
    )
    generated, info = model.transcribe(str(audio_path), vad_filter=True)
    pieces: list[str] = []
    segments: list[TranscriptSegmentResult] = []
    cursor = 0
    for raw in generated:
        segment_text = str(raw.text).strip()
        if not segment_text:
            continue
        text_start = cursor
        pieces.append(segment_text)
        cursor += len(segment_text) + 1
        segments.append(
            TranscriptSegmentResult(
                text=segment_text,
                start_ms=int(float(raw.start) * 1_000),
                end_ms=int(float(raw.end) * 1_000),
                speaker_id=None,
                detected_language=getattr(info, "language", None),
                confidence=None,
                confidence_source="unavailable",
                overlap_group_id=None,
                text_start=text_start,
                text_end=text_start + len(segment_text),
            )
        )
    result = TranscriptResult(
        text="\n".join(pieces),
        segments=segments,
        provider=LocalFasterWhisperProvider.provider_name,
        model=model_dir.name,
        detected_language=getattr(info, "language", None),
        warnings=("LOCAL_ASR_NO_DIARIZATION",),
    )
    return validate_transcript_result(result)


def result_payload(result: TranscriptResult) -> dict[str, object]:
    return {
        "text": result.text,
        "segments": [asdict(segment) for segment in result.segments],
        "provider": result.provider,
        "model": result.model,
        "detected_language": result.detected_language,
        "warnings": list(result.warnings),
    }
