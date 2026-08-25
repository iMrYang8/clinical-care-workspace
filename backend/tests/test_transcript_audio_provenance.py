import hashlib

import pytest

from app.services.voice.provenance import validate_fact_evidence


@pytest.mark.unit
def test_fact_requires_exact_transcript_hash_and_bounded_audio() -> None:
    transcript = "SPEAKER_00: Patient reports a penicillin allergy."
    quote = "penicillin allergy"
    start = transcript.index(quote)
    accepted = validate_fact_evidence(
        transcript=transcript,
        transcript_start=start,
        transcript_end=start + len(quote),
        exact_quote=quote,
        quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        segment_start_ms=2_000,
        segment_end_ms=5_000,
        audio_start_ms=2_100,
        audio_end_ms=4_900,
        asset_duration_ms=6_000,
    )
    assert accepted is True

    assert (
        validate_fact_evidence(
            transcript=transcript,
            transcript_start=start,
            transcript_end=start + len(quote),
            exact_quote=quote,
            quote_sha256="0" * 64,
            segment_start_ms=2_000,
            segment_end_ms=5_000,
            audio_start_ms=2_100,
            audio_end_ms=4_900,
            asset_duration_ms=6_000,
        )
        is False
    )

    assert (
        validate_fact_evidence(
            transcript=transcript,
            transcript_start=start,
            transcript_end=start + len(quote),
            exact_quote=quote,
            quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
            segment_start_ms=2_000,
            segment_end_ms=5_000,
            audio_start_ms=1_000,
            audio_end_ms=4_900,
            asset_duration_ms=6_000,
        )
        is False
    )
