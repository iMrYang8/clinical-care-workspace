import hashlib


def validate_fact_evidence(
    *,
    transcript: str,
    transcript_start: int,
    transcript_end: int,
    exact_quote: str,
    quote_sha256: str,
    segment_start_ms: int,
    segment_end_ms: int,
    audio_start_ms: int,
    audio_end_ms: int,
    asset_duration_ms: int,
) -> bool:
    """Return true only for a transcript span bound to a valid audio interval."""

    if (
        transcript_start < 0
        or transcript_end <= transcript_start
        or transcript_end > len(transcript)
        or transcript[transcript_start:transcript_end] != exact_quote
    ):
        return False
    if hashlib.sha256(exact_quote.encode()).hexdigest() != quote_sha256:
        return False
    if (
        segment_start_ms < 0
        or segment_end_ms <= segment_start_ms
        or audio_start_ms < segment_start_ms
        or audio_end_ms > segment_end_ms
        or audio_end_ms <= audio_start_ms
        or audio_end_ms > asset_duration_ms
    ):
        return False
    return True
