from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.services.voice.providers.base import TranscriptResult, TranscriptSegmentResult


@dataclass(frozen=True)
class DiarizationTurn:
    start_ms: int
    end_ms: int
    speaker_id: str
    confidence: float | None = None


def pyannote_runtime_status() -> tuple[bool, str]:
    """Report readiness without downloading gated weights."""

    if not settings.PYANNOTE_ENABLED:
        return False, "PYANNOTE_DISABLED"
    if not settings.PYANNOTE_MODEL_DIR:
        return False, "PYANNOTE_LOCAL_MODEL_REQUIRED"
    model_dir = Path(settings.PYANNOTE_MODEL_DIR).expanduser()
    if not model_dir.is_dir() or not any(model_dir.iterdir()):
        return False, "PYANNOTE_MODEL_NOT_CACHED"
    try:
        importlib.import_module("pyannote.audio")
    except ImportError:
        return False, "PYANNOTE_DEPENDENCY_UNAVAILABLE"
    return True, "PYANNOTE_LOCAL_MODEL_READY"


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass


class LocalPyannoteDiarizer:
    """Offline-only pyannote adapter executed in a killable child process."""

    def __init__(self, model_dir: str, *, timeout_seconds: float = 300) -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        if not self.model_dir.is_dir() or not any(self.model_dir.iterdir()):
            raise ValueError("PYANNOTE_MODEL_DIR must be a non-empty local directory")
        self.timeout_seconds = max(0.01, timeout_seconds)

    async def diarize(self, audio_path: Path) -> list[DiarizationTurn]:
        with tempfile.TemporaryDirectory(prefix="nightingale-diarization-") as name:
            temp_dir = Path(name)
            os.chmod(temp_dir, stat.S_IRWXU)
            result_path = temp_dir / "turns.json"
            environment = os.environ.copy()
            for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
                environment.pop(key, None)
            environment.update(
                {
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "HF_DATASETS_OFFLINE": "1",
                }
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.services.voice.pyannote_worker",
                    "--model-dir",
                    str(self.model_dir),
                    "--audio-path",
                    str(audio_path.resolve()),
                    "--result-path",
                    str(result_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=environment,
                )
            except OSError as exc:
                raise RuntimeError("LOCAL_DIARIZATION_PROCESS_UNAVAILABLE") from exc
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
                raise RuntimeError("LOCAL_DIARIZATION_PROCESS_FAILED")
            if result_path.stat().st_size > 4 * 1024 * 1024:
                raise RuntimeError("LOCAL_DIARIZATION_RESULT_TOO_LARGE")
            try:
                payload: Any = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ValueError
                turns = [
                    DiarizationTurn(
                        start_ms=int(item["start_ms"]),
                        end_ms=int(item["end_ms"]),
                        speaker_id=str(item["speaker_id"]),
                        confidence=(
                            float(item["confidence"])
                            if item.get("confidence") is not None
                            else None
                        ),
                    )
                    for item in payload
                    if isinstance(item, dict)
                ]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("LOCAL_DIARIZATION_RESULT_INVALID") from exc
            if any(
                turn.start_ms < 0
                or turn.end_ms <= turn.start_ms
                or not turn.speaker_id.strip()
                for turn in turns
            ):
                raise RuntimeError("LOCAL_DIARIZATION_RESULT_INVALID")
            return turns


def align_diarization_segments(
    segments: list[TranscriptSegmentResult],
    turns: list[DiarizationTurn],
) -> tuple[list[TranscriptSegmentResult], tuple[str, ...]]:
    """Align speakers by measured time overlap; never invent word timings."""

    output: list[TranscriptSegmentResult] = []
    warnings: set[str] = set()
    for index, segment in enumerate(segments):
        overlap_by_speaker: dict[str, int] = {}
        intersecting: list[DiarizationTurn] = []
        for turn in turns:
            overlap = min(segment.end_ms, turn.end_ms) - max(
                segment.start_ms, turn.start_ms
            )
            if overlap <= 0:
                continue
            intersecting.append(turn)
            overlap_by_speaker[turn.speaker_id] = (
                overlap_by_speaker.get(turn.speaker_id, 0) + overlap
            )
        if not overlap_by_speaker:
            output.append(segment)
            warnings.add("LOCAL_DIARIZATION_PARTIAL")
            continue
        speaker = max(
            overlap_by_speaker,
            key=lambda item: (overlap_by_speaker[item], item),
        )
        speaker_ids = tuple(
            sorted(
                overlap_by_speaker,
                key=lambda item: (-overlap_by_speaker[item], item),
            )
        )
        duration = max(1, segment.end_ms - segment.start_ms)
        coverage = sum(overlap_by_speaker.values()) / duration
        if coverage < 0.5:
            warnings.add("LOCAL_DIARIZATION_PARTIAL")
        simultaneous = any(
            left.speaker_id != right.speaker_id
            and min(left.end_ms, right.end_ms) > max(left.start_ms, right.start_ms)
            for left in intersecting
            for right in intersecting
        )
        overlap_group_id = segment.overlap_group_id
        if simultaneous:
            overlap_group_id = overlap_group_id or f"pyannote-overlap-{index + 1}"
            warnings.add("LOCAL_DIARIZATION_OVERLAP_REVIEW")
        output.append(
            replace(
                segment,
                speaker_id=speaker,
                speaker_ids=speaker_ids,
                overlap_group_id=overlap_group_id,
            )
        )
    return output, tuple(sorted(warnings))


def apply_local_diarization(
    result: TranscriptResult,
    turns: list[DiarizationTurn],
) -> TranscriptResult:
    segments, warnings = align_diarization_segments(result.segments, turns)
    retained = {
        warning for warning in result.warnings if warning != "LOCAL_ASR_NO_DIARIZATION"
    }
    return replace(
        result,
        segments=segments,
        warnings=tuple(sorted({*retained, *warnings})),
    )
