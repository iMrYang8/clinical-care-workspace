from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
    validate_transcript_result,
)


class OpenAIAudioTranscriptionProvider:
    provider_name = "openai"

    def __init__(self, *, api_key: str, model: str, timeout_seconds: int = 600) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, audio_path: Path) -> TranscriptResult:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            with audio_path.open("rb") as stream:
                content_type = {
                    ".flac": "audio/flac",
                    ".mp3": "audio/mpeg",
                    ".m4a": "audio/mp4",
                    ".wav": "audio/wav",
                }.get(audio_path.suffix.lower(), "application/octet-stream")
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (audio_path.name, stream, content_type)},
                    data={
                        "model": self.model,
                        "response_format": "diarized_json",
                        "chunking_strategy": "auto",
                    },
                )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise ValueError("transcription provider response is missing text")
        text = payload["text"]
        segments: list[TranscriptSegmentResult] = []
        warnings: set[str] = set()
        cursor = 0
        raw_segments = payload.get("segments", [])
        if not isinstance(raw_segments, list):
            raw_segments = []
        for raw in raw_segments:
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                continue
            segment_text = raw["text"].strip()
            if not segment_text:
                continue
            try:
                start_ms = int(float(raw.get("start", 0)) * 1_000)
                end_ms = int(float(raw.get("end", 0)) * 1_000)
            except (TypeError, ValueError):
                warnings.add("PROVIDER_DROPPED_INVALID_SEGMENT_RANGE")
                continue
            if start_ms < 0 or end_ms <= start_ms:
                warnings.add("PROVIDER_DROPPED_INVALID_SEGMENT_RANGE")
                continue
            start = text.find(segment_text, cursor)
            text_start = start if start >= 0 else None
            text_end = start + len(segment_text) if start >= 0 else None
            if text_end is not None:
                cursor = text_end
            confidence = (
                float(raw["confidence"])
                if isinstance(raw.get("confidence"), (float, int))
                else None
            )
            if confidence is not None and not 0 <= confidence <= 1:
                confidence = None
                warnings.add("PROVIDER_DROPPED_INVALID_CONFIDENCE")
            segments.append(
                TranscriptSegmentResult(
                    text=segment_text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker_id=(str(raw["speaker"]) if raw.get("speaker") else None),
                    detected_language=(
                        str(raw["language"]) if raw.get("language") else None
                    ),
                    confidence=confidence,
                    confidence_source=(
                        "provider" if confidence is not None else "unavailable"
                    ),
                    overlap_group_id=(
                        str(raw["overlap_group_id"])
                        if raw.get("overlap_group_id")
                        else None
                    ),
                    text_start=text_start,
                    text_end=text_end,
                )
            )
        if not segments:
            raise ValueError("transcription provider returned no timestamped segments")

        # Diarized responses can contain simultaneous speakers but do not
        # currently provide Nightingale's explicit overlap_group_id field.
        # Preserve those time ranges and label every intersecting segment
        # deterministically instead of rejecting or trimming source evidence.
        overlap_index = 0
        for current_index, current in enumerate(segments):
            intersections = [
                prior_index
                for prior_index, prior in enumerate(segments[:current_index])
                if current.start_ms < prior.end_ms and prior.start_ms < current.end_ms
            ]
            if not intersections:
                continue
            existing_groups: set[str] = set()
            for index in intersections:
                prior_group = segments[index].overlap_group_id
                if prior_group is not None:
                    existing_groups.add(prior_group)
            if current.overlap_group_id is not None:
                existing_groups.add(current.overlap_group_id)
            group_id = (
                sorted(existing_groups)[0]
                if existing_groups
                else f"openai-overlap-{overlap_index}"
            )
            if not existing_groups:
                overlap_index += 1
            for index in intersections:
                segments[index] = replace(segments[index], overlap_group_id=group_id)
            segments[current_index] = replace(
                segments[current_index], overlap_group_id=group_id
            )
        return validate_transcript_result(
            TranscriptResult(
                text=text,
                segments=segments,
                provider=self.provider_name,
                model=self.model,
                detected_language=(
                    str(payload["language"]) if payload.get("language") else None
                ),
                warnings=tuple(sorted(warnings)),
            )
        )
