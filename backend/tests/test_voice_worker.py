import uuid

import pytest

from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
    validate_transcript_result,
)
from app.services.voice.providers.deterministic import SyntheticFixtureProvider


@pytest.mark.unit
def test_fixture_provider_is_explicit_and_normalized() -> None:
    provider = SyntheticFixtureProvider()
    result = provider.transcribe_fixture("code-switch-overlap-v1")
    assert result.provider == "deterministic-synthetic-fixture"
    assert result.model == "code-switch-overlap-v1"
    assert [segment.speaker_id for segment in result.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert {segment.detected_language for segment in result.segments} == {"en", "zh"}
    assert result.segments[1].overlap_group_id == "overlap-1"
    validate_transcript_result(result)

    with pytest.raises(ValueError, match="Unknown synthetic fixture"):
        provider.transcribe_fixture("not-a-fixture")


@pytest.mark.unit
def test_invalid_transcript_segments_are_rejected() -> None:
    with pytest.raises(ValueError, match="segment time range"):
        validate_transcript_result(
            TranscriptResult(
                text="bad",
                segments=[
                    TranscriptSegmentResult(
                        text="bad",
                        start_ms=100,
                        end_ms=10,
                        speaker_id=None,
                        detected_language="en",
                        confidence=None,
                        confidence_source="unavailable",
                        overlap_group_id=None,
                    )
                ],
                provider="test",
                model="test",
            )
        )


@pytest.mark.unit
def test_result_contract_rejects_noncontiguous_text_offsets() -> None:
    result = TranscriptResult(
        text="hello world",
        segments=[
            TranscriptSegmentResult(
                text="hello",
                start_ms=0,
                end_ms=500,
                speaker_id=None,
                detected_language="en",
                confidence=0.9,
                confidence_source="provider",
                overlap_group_id=None,
                text_start=4,
                text_end=9,
            )
        ],
        provider="test",
        model=str(uuid.uuid4()),
    )
    with pytest.raises(ValueError, match="text span"):
        validate_transcript_result(result)
