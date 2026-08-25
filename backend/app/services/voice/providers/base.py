from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TranscriptSegmentResult:
    text: str
    start_ms: int
    end_ms: int
    speaker_id: str | None
    detected_language: str | None
    confidence: float | None
    confidence_source: str
    overlap_group_id: str | None
    text_start: int | None = None
    text_end: int | None = None


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    segments: list[TranscriptSegmentResult]
    provider: str
    model: str
    detected_language: str | None = None
    warnings: tuple[str, ...] = ()


class TranscriptionProvider(Protocol):
    provider_name: str

    async def transcribe(self, audio_path: Path) -> TranscriptResult: ...


def validate_transcript_result(result: TranscriptResult) -> TranscriptResult:
    """Validate provider output before any row or clinical fact is created."""

    if not result.provider.strip() or not result.model.strip():
        raise ValueError("provider and model identifiers are required")
    if not result.text.strip():
        raise ValueError("transcript text must not be empty")
    previous_end_ms = -1
    for segment in result.segments:
        if segment.start_ms < 0 or segment.end_ms <= segment.start_ms:
            raise ValueError("invalid segment time range")
        # Overlap is allowed; a segment that moves wholly backwards is not.
        if segment.end_ms <= previous_end_ms and segment.overlap_group_id is None:
            raise ValueError("segments must be chronological unless marked overlap")
        previous_end_ms = max(previous_end_ms, segment.end_ms)
        if segment.confidence is not None and not 0 <= segment.confidence <= 1:
            raise ValueError("segment confidence must be between zero and one")
        start = segment.text_start
        end = segment.text_end
        if (start is None) != (end is None):
            raise ValueError("both text span offsets are required")
        if start is not None and end is not None:
            if start < 0 or end <= start or result.text[start:end] != segment.text:
                raise ValueError("segment text span does not match transcript")
    return result
