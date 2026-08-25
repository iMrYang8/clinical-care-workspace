from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
    validate_transcript_result,
)


class LocalFasterWhisperProvider:
    provider_name = "faster-whisper-local"

    def __init__(self, model_dir: str) -> None:
        self.model_dir = Path(model_dir).expanduser().resolve()
        if not self.model_dir.is_dir() or not any(self.model_dir.iterdir()):
            raise ValueError("LOCAL_ASR_MODEL_DIR must be a non-empty local directory")

    async def transcribe(self, audio_path: Path) -> TranscriptResult:
        return await asyncio.to_thread(self._transcribe_sync, audio_path)

    def _transcribe_sync(self, audio_path: Path) -> TranscriptResult:
        try:
            whisper_module = importlib.import_module("faster_whisper")
        except ImportError as exc:
            raise RuntimeError("LOCAL_ASR_DEPENDENCY_UNAVAILABLE") from exc
        whisper_model: Any = whisper_module.WhisperModel
        # A concrete local path and local_files_only prevent runtime downloads.
        model: Any = whisper_model(
            str(self.model_dir),
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
            provider=self.provider_name,
            model=self.model_dir.name,
            detected_language=getattr(info, "language", None),
            warnings=("LOCAL_ASR_NO_DIARIZATION",),
        )
        return validate_transcript_result(result)
