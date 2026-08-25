from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
    validate_transcript_result,
)


class SyntheticFixtureProvider:
    """Fixed CI/demo transcript provider, gated by an explicit fixture ID.

    This is never selected for ordinary recordings. Keeping the fixture API
    separate from ``transcribe(audio_path)`` prevents fallback from being
    mistaken for actual speech recognition.
    """

    provider_name = "deterministic-synthetic-fixture"

    _FIXTURES = {
        "code-switch-overlap-v1": (
            (
                "Patient reports a penicillin allergy and says the rash started yesterday.",
                0,
                5_200,
                "SPEAKER_00",
                "en",
                0.96,
                None,
            ),
            (
                "医生：好的，我们今天会复核药物，也请注意 breathing difficulty.",
                4_800,
                10_200,
                "SPEAKER_01",
                "zh",
                0.68,
                "overlap-1",
            ),
        ),
    }

    def transcribe_fixture(self, fixture_id: str) -> TranscriptResult:
        fixture = self._FIXTURES.get(fixture_id)
        if fixture is None:
            raise ValueError("Unknown synthetic fixture")
        pieces = [item[0] for item in fixture]
        text = "\n".join(pieces)
        cursor = 0
        segments: list[TranscriptSegmentResult] = []
        for index, item in enumerate(fixture):
            segment_text, start_ms, end_ms, speaker, language, confidence, overlap = (
                item
            )
            text_start = cursor
            text_end = cursor + len(segment_text)
            segments.append(
                TranscriptSegmentResult(
                    text=segment_text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker_id=speaker,
                    detected_language=language,
                    confidence=confidence,
                    confidence_source="synthetic_fixture",
                    overlap_group_id=overlap,
                    text_start=text_start,
                    text_end=text_end,
                )
            )
            cursor = text_end + (1 if index < len(fixture) - 1 else 0)
        return validate_transcript_result(
            TranscriptResult(
                text=text,
                segments=segments,
                provider=self.provider_name,
                model=fixture_id,
                detected_language="multilingual",
                warnings=("SYNTHETIC_FIXTURE", "OVERLAP_REVIEW"),
            )
        )
