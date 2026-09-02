from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.services.voice.language import (
    AddressableLanguageSpan,
    validate_addressable_language_spans,
)


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
    # `detected_language` is retained for provider compatibility. These fields
    # preserve the source-language claim separately from ASR word confidence.
    source_language: str | None = None
    language_confidence: float | None = None
    # All intersecting diarization speakers remain addressable. `speaker_id`
    # is the compatibility primary (largest measured overlap), never the only
    # evidence when simultaneous speakers were detected.
    speaker_ids: tuple[str, ...] = ()
    # Complete segment-relative intervals preserve code-switches without
    # rewriting or splitting the immutable source text.
    language_spans: tuple[AddressableLanguageSpan, ...] = ()


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
    if not result.segments:
        raise ValueError("at least one transcript segment is required")
    previous_end_ms = -1
    for segment in result.segments:
        if segment.start_ms < 0 or segment.end_ms <= segment.start_ms:
            raise ValueError("invalid segment time range")
        # Every time intersection must be explicit.  Merely ending after the
        # previous segment does not make an unlabelled overlap trustworthy.
        if segment.start_ms < previous_end_ms and segment.overlap_group_id is None:
            raise ValueError("segments must be chronological unless marked overlap")
        previous_end_ms = max(previous_end_ms, segment.end_ms)
        if segment.confidence is not None and not 0 <= segment.confidence <= 1:
            raise ValueError("segment confidence must be between zero and one")
        if (
            segment.language_confidence is not None
            and not 0 <= segment.language_confidence <= 1
        ):
            raise ValueError("language confidence must be between zero and one")
        if any(not item.strip() for item in segment.speaker_ids):
            raise ValueError("speaker identifiers must not be empty")
        if len(set(segment.speaker_ids)) != len(segment.speaker_ids):
            raise ValueError("speaker identifiers must be unique")
        if segment.speaker_id is not None and segment.speaker_ids:
            if segment.speaker_id not in segment.speaker_ids:
                raise ValueError(
                    "primary speaker must be retained in speaker identifiers"
                )
        validate_addressable_language_spans(segment.text, segment.language_spans)
        start = segment.text_start
        end = segment.text_end
        if (start is None) != (end is None):
            raise ValueError("both text span offsets are required")
        if start is not None and end is not None:
            if start < 0 or end <= start or result.text[start:end] != segment.text:
                raise ValueError("segment text span does not match transcript")
    return result
