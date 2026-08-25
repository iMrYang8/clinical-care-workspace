"""Transcription provider adapters.

Providers return the same normalized result. Optional providers are imported
inside their methods so the default model-free demo never downloads weights.
"""

from app.services.voice.providers.base import (
    TranscriptResult,
    TranscriptSegmentResult,
    TranscriptionProvider,
)

__all__ = [
    "TranscriptResult",
    "TranscriptSegmentResult",
    "TranscriptionProvider",
]
