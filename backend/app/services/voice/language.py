from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Literal, cast

from sqlmodel import Session, select

from app.models import ClinicOperationalSetting

LanguageCode = Literal["en", "ms", "nan", "zh", "cmn", "und"]
LanguageDetectionSource = Literal[
    "provider_hint",
    "lexicon_rule",
    "lexicon_and_provider",
    "mixed_rule",
    "unavailable",
    "human_review",
]

SUPPORTED_LANGUAGE_CODES = frozenset({"en", "ms", "nan", "zh", "cmn", "und"})
CLINIC_CONFIGURABLE_LANGUAGE_CODES: frozenset[LanguageCode] = frozenset(
    {"en", "ms", "nan", "zh", "cmn"}
)
LANGUAGE_DETECTION_SOURCES = frozenset(
    {
        "provider_hint",
        "lexicon_rule",
        "lexicon_and_provider",
        "mixed_rule",
        "unavailable",
        "human_review",
    }
)

# Provider language hints below the same review threshold used by the voice
# review surface are evidence, not a qualified language determination.  The
# original text and confidence remain addressable, while downstream clinical
# extraction fails closed.
LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD = 0.85


@dataclass(frozen=True)
class AddressableLanguageSpan:
    """A complete, segment-relative source-language interval."""

    start_offset: int
    end_offset: int
    language_code: LanguageCode
    confidence: float | None
    detection_source: LanguageDetectionSource
    review_required: bool = False


def configured_language_codes(values: object) -> frozenset[LanguageCode]:
    """Parse one clinic's exact language allowlist, failing closed.

    Operational settings are persisted as JSON. Treat a missing, malformed, or
    partly unsupported value as an empty policy rather than silently restoring
    product defaults. ``und`` is a detection result, never an enabled language.
    """

    if not isinstance(values, list) or not values:
        return frozenset()
    if any(
        not isinstance(value, str) or value not in CLINIC_CONFIGURABLE_LANGUAGE_CODES
        for value in values
    ):
        return frozenset()
    return frozenset(cast(LanguageCode, value) for value in values)


def clinic_supported_language_codes(
    db: Session, clinic_id: uuid.UUID
) -> frozenset[LanguageCode]:
    """Load the runtime clinic policy used by batch and live voice paths."""

    setting = db.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == clinic_id
        )
    ).first()
    if setting is None:
        return frozenset()
    return configured_language_codes(setting.supported_languages_json)


def apply_clinic_language_policy(
    spans: tuple[AddressableLanguageSpan, ...],
    supported_languages: frozenset[LanguageCode],
) -> tuple[AddressableLanguageSpan, ...]:
    """Mark disallowed/unknown spans for review without rewriting the source."""

    return tuple(
        replace(
            span,
            review_required=(
                span.review_required
                or span.language_code == "und"
                or span.language_code not in supported_languages
                or (
                    span.detection_source == "provider_hint"
                    and (
                        span.confidence is None
                        or span.confidence < LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD
                    )
                )
                or (
                    span.detection_source == "lexicon_and_provider"
                    and span.confidence is not None
                    and span.confidence < LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD
                )
            ),
        )
        for span in spans
    )


def validate_addressable_language_spans(
    text: str,
    spans: tuple[AddressableLanguageSpan, ...],
) -> tuple[AddressableLanguageSpan, ...]:
    """Require ordered, gap-free coverage without changing source text."""

    if not spans:
        return spans
    cursor = 0
    for span in spans:
        if (
            span.start_offset != cursor
            or span.end_offset <= span.start_offset
            or span.end_offset > len(text)
            or span.language_code not in SUPPORTED_LANGUAGE_CODES
            or span.detection_source not in LANGUAGE_DETECTION_SOURCES
            or (span.confidence is not None and not 0.0 <= span.confidence <= 1.0)
        ):
            raise ValueError("invalid addressable language span")
        cursor = span.end_offset
    if cursor != len(text):
        raise ValueError("language spans must cover the complete segment text")
    return spans


def language_span_payload(span: AddressableLanguageSpan) -> dict[str, object]:
    return {
        "start_offset": span.start_offset,
        "end_offset": span.end_offset,
        "language_code": span.language_code,
        "confidence": span.confidence,
        "detection_source": span.detection_source,
        "review_required": span.review_required,
    }


def language_span_from_payload(payload: object) -> AddressableLanguageSpan:
    if not isinstance(payload, dict):
        raise ValueError("language span payload must be an object")
    start = payload.get("start_offset")
    end = payload.get("end_offset")
    language = payload.get("language_code")
    confidence = payload.get("confidence")
    detection_source = payload.get("detection_source")
    review_required = payload.get("review_required", False)
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(language, str)
        or language not in SUPPORTED_LANGUAGE_CODES
        or not isinstance(detection_source, str)
        or detection_source not in LANGUAGE_DETECTION_SOURCES
        or not isinstance(review_required, bool)
        or (
            confidence is not None
            and (
                isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            )
        )
    ):
        raise ValueError("invalid language span payload")
    return AddressableLanguageSpan(
        start_offset=start,
        end_offset=end,
        language_code=cast(LanguageCode, language),
        confidence=float(confidence) if confidence is not None else None,
        detection_source=cast(LanguageDetectionSource, detection_source),
        review_required=review_required,
    )
