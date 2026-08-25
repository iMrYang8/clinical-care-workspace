from __future__ import annotations

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

    def __init__(self, *, api_key: str, model: str, timeout_seconds: int = 180) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, audio_path: Path) -> TranscriptResult:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            with audio_path.open("rb") as stream:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (audio_path.name, stream, "audio/wav")},
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
        cursor = 0
        raw_segments = payload.get("segments", [])
        if not isinstance(raw_segments, list):
            raw_segments = []
        for raw in raw_segments:
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                continue
            segment_text = raw["text"].strip()
            start = text.find(segment_text, cursor)
            text_start = start if start >= 0 else None
            text_end = start + len(segment_text) if start >= 0 else None
            if text_end is not None:
                cursor = text_end
            segments.append(
                TranscriptSegmentResult(
                    text=segment_text,
                    start_ms=int(float(raw.get("start", 0)) * 1_000),
                    end_ms=int(float(raw.get("end", 0)) * 1_000),
                    speaker_id=(str(raw["speaker"]) if raw.get("speaker") else None),
                    detected_language=(
                        str(raw["language"]) if raw.get("language") else None
                    ),
                    confidence=(
                        float(raw["confidence"])
                        if isinstance(raw.get("confidence"), (float, int))
                        else None
                    ),
                    confidence_source=(
                        "provider"
                        if raw.get("confidence") is not None
                        else "unavailable"
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
        return validate_transcript_result(
            TranscriptResult(
                text=text,
                segments=segments,
                provider=self.provider_name,
                model=self.model,
                detected_language=(
                    str(payload["language"]) if payload.get("language") else None
                ),
            )
        )
