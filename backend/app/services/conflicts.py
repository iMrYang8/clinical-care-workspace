from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    ClinicalFactAssertion,
    ConflictCase,
    Entry,
    EntryVersion,
    Highlight,
    ProvenancePointer,
)
from app.services.clinical_formulary import (
    AllergyCategory,
    allergy_category_for_assertion,
    canonicalize_allergy_concept,
    canonicalize_medication,
)
from app.services.decisioning import create_assertion
from app.services.voice.language import (
    LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD,
    LanguageCode,
    LanguageDetectionSource,
)


@dataclass(frozen=True)
class NormalizedFact:
    fact_type: str
    key: str
    value: str
    start: int
    end: int
    quote: str
    assertion_scope: Literal[
        "specific_substance", "drug_allergies", "all_allergies"
    ] = "specific_substance"
    polarity: Literal["present", "absent", "unknown"] = "present"
    allergy_category: AllergyCategory | None = None
    source_language: str = "und"
    review_required: bool = False


@dataclass(frozen=True)
class LanguageSpan:
    """Addressable source-language span; ``und`` is the mixed safe fallback."""

    start: int
    end: int
    source_language: LanguageCode
    confidence: float | None = None
    detection_source: LanguageDetectionSource = "unavailable"
    review_required: bool = False


# The deterministic lexicon is deliberately small. Unsupported or uncertain
# wording remains review-required evidence rather than being promoted to a
# reassuring "no allergy" assertion.
_SUPPORTED_LANGUAGES = {"en", "ms", "nan", "zh", "cmn"}


@dataclass(frozen=True)
class _AllergyPattern:
    pattern: re.Pattern[str]
    scope: Literal["specific_substance", "drug_allergies", "all_allergies"]
    polarity: Literal["present", "absent", "unknown"]
    language: str
    subject_group: int | None = None


_ALLERGY_PATTERNS: tuple[_AllergyPattern, ...] = (
    # English broad-scope statements precede specific extraction.
    _AllergyPattern(
        re.compile(
            r"\b(?:nkda|no\s+known\s+drug\s+allerg(?:y|ies)(?!\s+to\b))\b",
            re.I,
        ),
        "drug_allergies",
        "absent",
        "en",
    ),
    _AllergyPattern(
        re.compile(r"\b(?:nka|no\s+known\s+allerg(?:y|ies)(?!\s+to\b))\b", re.I),
        "all_allergies",
        "absent",
        "en",
    ),
    _AllergyPattern(
        re.compile(
            r"\b(?:allerg(?:y|ies)\s+(?:are\s+)?not\s+documented|"
            r"allergy\s+status\s+(?:is\s+)?unknown|no\s+allergy\s+information)\b",
            re.I,
        ),
        "all_allergies",
        "unknown",
        "en",
    ),
    _AllergyPattern(
        re.compile(
            r"\bno\s+(?:known\s+)?(?:drug\s+)?allerg(?:y|ic)\s+to\s+"
            r"([a-z][a-z0-9-]+)",
            re.I,
        ),
        "specific_substance",
        "absent",
        "en",
        1,
    ),
    _AllergyPattern(
        re.compile(r"\b(?:allergic\s+to|allergy\s+to)\s+([a-z][a-z0-9-]+)", re.I),
        "specific_substance",
        "present",
        "en",
        1,
    ),
    _AllergyPattern(
        re.compile(r"\b([a-z][a-z0-9-]+)\s+allergy\b", re.I),
        "specific_substance",
        "present",
        "en",
        1,
    ),
    # Bahasa Melayu.
    _AllergyPattern(
        re.compile(r"\btiada\s+alahan\s+ubat\s+(?:yang\s+)?diketahui\b", re.I),
        "drug_allergies",
        "absent",
        "ms",
    ),
    _AllergyPattern(
        re.compile(r"\btiada\s+alahan\s+(?:yang\s+)?diketahui\b", re.I),
        "all_allergies",
        "absent",
        "ms",
    ),
    _AllergyPattern(
        re.compile(r"\b(?:alahan|alergi)\s+tidak\s+didokumenkan\b", re.I),
        "all_allergies",
        "unknown",
        "ms",
    ),
    _AllergyPattern(
        re.compile(
            r"\btiada\s+(?:alahan|alergi)\s+(?:kepada|terhadap)\s+"
            r"([a-z][a-z0-9-]+)",
            re.I,
        ),
        "specific_substance",
        "absent",
        "ms",
        1,
    ),
    _AllergyPattern(
        re.compile(
            r"\b(?:alahan|alergi)\s+(?:kepada|terhadap)\s+([a-z][a-z0-9-]+)", re.I
        ),
        "specific_substance",
        "present",
        "ms",
        1,
    ),
    # POJ-style Hokkien/Taiwanese (nan). Text is accent-folded below.
    _AllergyPattern(
        re.compile(r"\bbo\s+chai\s+e\s+(?:koe-bin|koe bin)\b", re.I),
        "all_allergies",
        "absent",
        "nan",
    ),
    _AllergyPattern(
        re.compile(
            r"\bbo\s+(?:tui|ti)\s+([a-z][a-z0-9-]+)\s+(?:koe-bin|koe bin)\b", re.I
        ),
        "specific_substance",
        "absent",
        "nan",
        1,
    ),
    _AllergyPattern(
        re.compile(r"\b(?:tui|ti)\s+([a-z][a-z0-9-]+)\s+(?:koe-bin|koe bin)\b", re.I),
        "specific_substance",
        "present",
        "nan",
        1,
    ),
    # Existing Chinese support stays deterministic and source-addressable.
    _AllergyPattern(
        re.compile(r"(?:无|無)已知(?:的)?(?:药物|藥物)(?:过敏|過敏)"),
        "drug_allergies",
        "absent",
        "zh",
    ),
    _AllergyPattern(
        re.compile(r"(?:无|無)已知(?:的)?(?:过敏|過敏)"),
        "all_allergies",
        "absent",
        "zh",
    ),
    _AllergyPattern(
        re.compile(r"过敏(?:史|信息)?(?:未记录|不详)|過敏(?:史|資訊)?(?:未記錄|不詳)"),
        "all_allergies",
        "unknown",
        "zh",
    ),
    _AllergyPattern(
        re.compile(r"(?:对|對)(青霉素|青黴素)不(?:过敏|過敏)"),
        "specific_substance",
        "absent",
        "zh",
        1,
    ),
    _AllergyPattern(
        re.compile(r"(?:对|對)(青霉素|青黴素)(?:过敏|過敏)"),
        "specific_substance",
        "present",
        "zh",
        1,
    ),
)
_ALLERGY_KEYWORD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\ballerg(?:y|ies|ic|ie)?\b", re.I), "en"),
    (re.compile(r"\b(?:alahan|alergi)\b", re.I), "ms"),
    (re.compile(r"\b(?:koe-bin|koe bin)\b", re.I), "nan"),
    (re.compile(r"(?:过敏|過敏)"), "zh"),
)


@dataclass(frozen=True)
class _MedicationPattern:
    pattern: re.Pattern[str]
    language: str
    stopped_actions: frozenset[str]


@dataclass(frozen=True)
class _DosePattern:
    pattern: re.Pattern[str]
    language: str


_MEDICATION_PATTERNS: tuple[_MedicationPattern, ...] = (
    _MedicationPattern(
        re.compile(
            r"\b(?P<action>started|start|taking|continue|continued|stopped|stop|"
            r"discontinued)\s+(?P<med>[a-z][a-z0-9-]{2,})\b",
            re.I,
        ),
        "en",
        frozenset({"stopped", "stop", "discontinued"}),
    ),
    _MedicationPattern(
        re.compile(
            r"\b(?P<action>mula|mulakan|mengambil|ambil|teruskan|hentikan|"
            r"berhenti)\s+(?P<med>[a-z][a-z0-9-]{2,})\b",
            re.I,
        ),
        "ms",
        frozenset({"hentikan", "berhenti"}),
    ),
    # POJ-style Hokkien/Taiwanese; the input is accent-folded before matching.
    _MedicationPattern(
        re.compile(
            r"\b(?P<action>kha-si|kha si|chiah|ke-sok|ke sok|theng)\s+"
            r"(?P<med>[a-z][a-z0-9-]{2,})\b",
            re.I,
        ),
        "nan",
        frozenset({"theng"}),
    ),
    _MedicationPattern(
        re.compile(
            r"(?P<action>开始服用|開始服用|开始|開始|服用|继续服用|繼續服用|"
            r"停用|停止服用)(?P<med>[\u3400-\u9fff]{2,8})"
        ),
        "zh",
        frozenset({"停用", "停止服用"}),
    ),
)
# Action verbs also occur in ordinary narrative (for example, "the rash
# started yesterday" or "continue the reviewed care plan"). These bounded
# non-drug objects prevent the deterministic extractor from manufacturing a
# medication assertion that would incorrectly block the publication gate.
_NON_MEDICATION_ACTION_OBJECTS = {
    "a",
    "an",
    "bedside",
    "care",
    "current",
    "earlier",
    "exercises",
    "fall-risk",
    "glucose",
    "later",
    "monitoring",
    "physiotherapy",
    "plan",
    "recently",
    "strengthening",
    "the",
    "today",
    "tomorrow",
    "yesterday",
}
_DOSE_PATTERNS: tuple[_DosePattern, ...] = (
    _DosePattern(
        re.compile(
            r"\b(?P<med>[a-z][a-z0-9-]{2,})\s+"
            r"(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g|ml)"
            r"(?:\s+(?P<route>po|oral|iv|intravenous|im|subcutaneous))?"
            r"(?:\s+(?P<frequency>daily|once daily|twice daily|three times daily|"
            r"four times daily|bid|tid|qid|qd))?\b",
            re.I,
        ),
        "en",
    ),
    _DosePattern(
        re.compile(
            r"\b(?P<med>[a-z][a-z0-9-]{2,})\s+"
            r"(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g|ml)"
            r"(?:\s+(?P<route>secara\s+oral|melalui\s+mulut|intravena|"
            r"intramuskular|subkutaneus))?"
            r"(?:\s+(?P<frequency>sekali\s+sehari|dua\s+kali\s+sehari|"
            r"tiga\s+kali\s+sehari|empat\s+kali\s+sehari))?\b",
            re.I,
        ),
        "ms",
    ),
    _DosePattern(
        re.compile(
            r"\b(?P<med>[a-z][a-z0-9-]{2,})\s+"
            r"(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g|ml)"
            r"(?:\s+(?P<route>khau-hok|khau hok|chhui))?"
            r"(?:\s+(?P<frequency>chit\s+jit\s+chit\s+pai|"
            r"chit\s+jit\s+nng\s+pai))?\b",
            re.I,
        ),
        "nan",
    ),
    _DosePattern(
        re.compile(
            r"(?P<med>二甲双胍|二甲雙胍|阿莫西林|阿司匹林)\s*"
            r"(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>毫克|克|毫升)"
            r"(?:\s*(?P<route>口服|静脉|靜脈|肌肉注射|皮下注射))?"
            r"(?:\s*(?P<frequency>(?:每日|每天)(?:一|两|兩|二|三|四)次))?"
        ),
        "zh",
    ),
)

_ROUTES = {
    "po": "oral",
    "oral": "oral",
    "iv": "intravenous",
    "intravenous": "intravenous",
    "im": "intramuscular",
    "subcutaneous": "subcutaneous",
    "secara oral": "oral",
    "melalui mulut": "oral",
    "intravena": "intravenous",
    "intramuskular": "intramuscular",
    "subkutaneus": "subcutaneous",
    "khau-hok": "oral",
    "khau hok": "oral",
    "chhui": "oral",
    "口服": "oral",
    "静脉": "intravenous",
    "靜脈": "intravenous",
    "肌肉注射": "intramuscular",
    "皮下注射": "subcutaneous",
}
_FREQUENCIES = {
    "qd": "once_daily",
    "daily": "once_daily",
    "once daily": "once_daily",
    "bid": "twice_daily",
    "twice daily": "twice_daily",
    "tid": "three_times_daily",
    "three times daily": "three_times_daily",
    "qid": "four_times_daily",
    "four times daily": "four_times_daily",
    "sekali sehari": "once_daily",
    "dua kali sehari": "twice_daily",
    "tiga kali sehari": "three_times_daily",
    "empat kali sehari": "four_times_daily",
    "chit jit chit pai": "once_daily",
    "chit jit nng pai": "twice_daily",
    "每日一次": "once_daily",
    "每天一次": "once_daily",
    "每日两次": "twice_daily",
    "每日兩次": "twice_daily",
    "每天两次": "twice_daily",
    "每天兩次": "twice_daily",
    "每日三次": "three_times_daily",
    "每天三次": "three_times_daily",
    "每日四次": "four_times_daily",
    "每天四次": "four_times_daily",
}


def _dose_value(value: str, unit: str) -> str:
    numeric = float(value)
    lowered = unit.lower()
    if lowered in {"g", "克"}:
        numeric *= 1_000
        lowered = "mg"
    elif lowered == "mcg":
        numeric /= 1_000
        lowered = "mg"
    elif lowered == "毫克":
        lowered = "mg"
    elif lowered == "毫升":
        lowered = "ml"
    return f"{numeric:g}{lowered}"


def _dose_number(value: str) -> float | None:
    match = re.match(r"[0-9.]+", value)
    return float(match.group(0)) if match else None


def normalize_language_code(value: str | None) -> LanguageCode:
    if not value:
        return "und"
    normalized = value.strip().lower().replace("_", "-")
    primary = normalized.split("-", 1)[0]
    return cast(LanguageCode, primary if primary in _SUPPORTED_LANGUAGES else "und")


def _accent_fold_preserving_offsets(value: str) -> str:
    """Fold accents one code point at a time without shifting source offsets."""

    output: list[str] = []
    for character in value:
        decomposed = unicodedata.normalize("NFKD", character)
        base = "".join(item for item in decomposed if not unicodedata.combining(item))
        # A compatibility expansion would invalidate exact-source offsets.
        output.append(base if len(base) == 1 else character)
    return "".join(output)


def _canonical_substance(
    value: str,
) -> tuple[str, AllergyCategory | None, bool]:
    normalized = value.strip().casefold()
    concept = canonicalize_allergy_concept(normalized)
    if concept is not None:
        return concept.canonical_name, concept.category, False
    return normalized, None, True


def _canonical_medication(value: str) -> tuple[str, bool]:
    normalized = value.strip().casefold()
    concept = canonicalize_medication(normalized)
    if concept is None:
        return normalized, True
    return concept.display_name, False


_LANGUAGE_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:alahan|alergi|tiada|kepada|terhadap|mula|mulakan|mengambil|"
            r"ambil|teruskan|hentikan|berhenti|sekali|dua|tiga|empat|sehari)\b",
            re.I,
        ),
        "ms",
    ),
    (
        re.compile(
            r"\b(?:bo|tui|ti|koe-bin|koe bin|chiah|kha-si|kha si|ke-sok|ke sok|theng|"
            r"chit jit|nng pai|khau-hok|khau hok|chhui)\b",
            re.I,
        ),
        "nan",
    ),
    (re.compile(r"[\u3400-\u9fff]+"), "zh"),
    (
        re.compile(
            r"\b(?:allerg(?:y|ies|ic)|started|start|taking|continue|continued|"
            r"stopped|stop|discontinued)\b",
            re.I,
        ),
        "en",
    ),
)


def detect_language_spans(
    content: str,
    *,
    source_language: str | None = None,
    source_confidence: float | None = None,
) -> list[LanguageSpan]:
    """Return gap-free, exact-offset language spans for mixed clinical text.

    Sentence punctuation is the first boundary. When one sentence contains
    multiple qualified lexicons, a transition begins at the first marker of
    the new language so match-level evidence remains addressable. Ambiguous
    simultaneous markers still fail closed to ``und``.
    """

    if not content:
        return []
    fallback = normalize_language_code(source_language)
    qualified_confidence = (
        source_confidence
        if source_confidence is not None and 0.0 <= source_confidence <= 1.0
        else None
    )
    folded = _accent_fold_preserving_offsets(content)
    spans: list[LanguageSpan] = []
    boundaries = [0]
    boundaries.extend(match.end() for match in re.finditer(r"[.!?。！？;；]+", content))
    if boundaries[-1] != len(content):
        boundaries.append(len(content))
    start = 0
    for end in boundaries[1:]:
        if end <= start:
            continue
        fragment = folded[start:end]
        detected = {
            language
            for pattern, language in _LANGUAGE_MARKERS
            if pattern.search(fragment)
        }
        if len(detected) == 1:
            language = cast(LanguageCode, next(iter(detected)))
            provider_agrees = fallback != "und" and fallback == language
            confidence = qualified_confidence if provider_agrees else None
            review_required = bool(
                provider_agrees
                and confidence is not None
                and confidence < LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD
            )
            detection_source: LanguageDetectionSource = (
                "lexicon_and_provider" if provider_agrees else "lexicon_rule"
            )
        elif len(detected) > 1:
            marker_hits = sorted(
                (
                    match.start(),
                    match.end(),
                    language,
                )
                for pattern, language in _LANGUAGE_MARKERS
                for match in pattern.finditer(fragment)
            )
            ambiguous_offsets = any(
                left[0] == right[0] and left[2] != right[2]
                for left, right in zip(marker_hits, marker_hits[1:], strict=False)
            )
            if marker_hits and not ambiguous_offsets:
                span_start = start
                current_language = cast(LanguageCode, marker_hits[0][2])
                for marker_start, _marker_end, marker_language in marker_hits[1:]:
                    if marker_language == current_language:
                        continue
                    boundary = start + marker_start
                    if boundary > span_start:
                        provider_agrees = (
                            fallback != "und" and fallback == current_language
                        )
                        spans.append(
                            LanguageSpan(
                                start=span_start,
                                end=boundary,
                                source_language=current_language,
                                confidence=(
                                    qualified_confidence if provider_agrees else None
                                ),
                                detection_source=(
                                    "lexicon_and_provider"
                                    if provider_agrees
                                    else "lexicon_rule"
                                ),
                                review_required=bool(
                                    provider_agrees
                                    and qualified_confidence is not None
                                    and qualified_confidence
                                    < LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD
                                ),
                            )
                        )
                        span_start = boundary
                    current_language = cast(LanguageCode, marker_language)
                provider_agrees = fallback != "und" and fallback == current_language
                spans.append(
                    LanguageSpan(
                        start=span_start,
                        end=end,
                        source_language=current_language,
                        confidence=(qualified_confidence if provider_agrees else None),
                        detection_source=(
                            "lexicon_and_provider"
                            if provider_agrees
                            else "lexicon_rule"
                        ),
                        review_required=bool(
                            provider_agrees
                            and qualified_confidence is not None
                            and qualified_confidence
                            < LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD
                        ),
                    )
                )
                start = end
                continue
            language = "und"
            review_required = True
            confidence = None
            detection_source = "mixed_rule"
        else:
            language = fallback
            confidence = qualified_confidence if language != "und" else None
            review_required = bool(
                language == "und"
                or confidence is None
                or confidence < LANGUAGE_CONFIDENCE_REVIEW_THRESHOLD
            )
            detection_source = "provider_hint" if language != "und" else "unavailable"
        spans.append(
            LanguageSpan(
                start=start,
                end=end,
                source_language=language,
                confidence=confidence,
                detection_source=detection_source,
                review_required=review_required,
            )
        )
        start = end
    return spans


def extract_normalized_facts(
    content: str, *, source_language: str | None = None
) -> list[NormalizedFact]:
    facts: list[NormalizedFact] = []
    requested_language = normalize_language_code(source_language)
    searchable = _accent_fold_preserving_offsets(content)
    occupied_spans: list[tuple[int, int]] = []
    for allergy_definition in _ALLERGY_PATTERNS:
        for match in allergy_definition.pattern.finditer(searchable):
            # Prefer a broad statement over a substring-specific pattern.
            if any(
                match.start() >= start and match.end() <= end
                for start, end in occupied_spans
            ):
                continue
            if allergy_definition.polarity == "present" and re.search(
                r"\bno\s+(?:known\s+)?(?:drug\s+)?$",
                searchable[max(0, match.start() - 24) : match.start()],
                re.I,
            ):
                continue
            if allergy_definition.subject_group is None:
                key = "*" if allergy_definition.scope == "all_allergies" else "*drug"
                allergy_category = allergy_category_for_assertion(
                    key, allergy_definition.scope
                )
                unsupported = False
            else:
                key, allergy_category, unsupported = _canonical_substance(
                    content[
                        match.start(allergy_definition.subject_group) : match.end(
                            allergy_definition.subject_group
                        )
                    ]
                )
            review_required = allergy_definition.polarity == "unknown" or unsupported
            polarity: Literal["present", "absent", "unknown"] = (
                allergy_definition.polarity
            )
            # Language-specific negation is only trusted when the ASR supplied
            # a supported language code. Unknown languages remain uncertain.
            if requested_language == "und" and source_language:
                polarity = "unknown"
                review_required = True
            facts.append(
                NormalizedFact(
                    fact_type="allergy",
                    key=key,
                    value=polarity,
                    start=match.start(),
                    end=match.end(),
                    quote=content[match.start() : match.end()],
                    assertion_scope=allergy_definition.scope,
                    polarity=polarity,
                    allergy_category=allergy_category,
                    # The exact phrase, rather than a recording-level hint,
                    # determines span language. This keeps code-switched
                    # allergy evidence addressable instead of filtering it.
                    source_language=(
                        "und"
                        if requested_language == "und" and source_language
                        else allergy_definition.language
                    ),
                    review_required=review_required,
                )
            )
            occupied_spans.append((match.start(), match.end()))
    # An allergy-relevant phrase outside the qualified deterministic patterns
    # is evidence of uncertainty, not evidence of absence. Preserve the exact
    # keyword span so a clinician can review the original wording.
    for keyword_pattern, keyword_language in _ALLERGY_KEYWORD_PATTERNS:
        for match in keyword_pattern.finditer(searchable):
            if any(
                match.start() >= start and match.end() <= end
                for start, end in occupied_spans
            ):
                continue
            facts.append(
                NormalizedFact(
                    fact_type="allergy",
                    key="*",
                    value="unknown",
                    start=match.start(),
                    end=match.end(),
                    quote=content[match.start() : match.end()],
                    assertion_scope="all_allergies",
                    polarity="unknown",
                    allergy_category=None,
                    source_language=(
                        "und"
                        if requested_language == "und" and source_language
                        else keyword_language
                    ),
                    review_required=True,
                )
            )
            occupied_spans.append((match.start(), match.end()))
    dose_matches = [
        (dose_definition, match)
        for dose_definition in _DOSE_PATTERNS
        for match in dose_definition.pattern.finditer(searchable)
    ]
    best_dose_matches: dict[
        tuple[int, str, str], tuple[_DosePattern, re.Match[str]]
    ] = {}
    for dose_definition, match in dose_matches:
        dose_key = (
            match.start("med"),
            match.group("dose"),
            match.group("unit").casefold(),
        )
        previous = best_dose_matches.get(dose_key)
        score = (
            match.end() - match.start(),
            dose_definition.language == requested_language,
        )
        if previous is None:
            best_dose_matches[dose_key] = (dose_definition, match)
            continue
        old_definition, old_match = previous
        old_score = (
            old_match.end() - old_match.start(),
            old_definition.language == requested_language,
        )
        if score > old_score:
            best_dose_matches[dose_key] = (dose_definition, match)
    for dose_definition, match in best_dose_matches.values():
        medication, unsupported = _canonical_medication(
            content[match.start("med") : match.end("med")]
        )
        quote = content[match.start() : match.end()]
        facts.append(
            NormalizedFact(
                fact_type="dose",
                key=medication,
                value=_dose_value(match.group("dose"), match.group("unit")),
                start=match.start(),
                end=match.end(),
                quote=quote,
                source_language=dose_definition.language,
                review_required=unsupported,
            )
        )
        route = match.groupdict().get("route")
        if route:
            route_key = route.casefold()
            facts.append(
                NormalizedFact(
                    fact_type="route",
                    key=medication,
                    value=_ROUTES.get(route_key, route_key),
                    start=match.start(),
                    end=match.end(),
                    quote=quote,
                    source_language=dose_definition.language,
                    review_required=unsupported,
                )
            )
        frequency = match.groupdict().get("frequency")
        if frequency:
            frequency_key = frequency.casefold()
            facts.append(
                NormalizedFact(
                    fact_type="frequency",
                    key=medication,
                    value=_FREQUENCIES.get(
                        frequency_key, frequency_key.replace(" ", "_")
                    ),
                    start=match.start(),
                    end=match.end(),
                    quote=quote,
                    source_language=dose_definition.language,
                    review_required=unsupported,
                )
            )
    for medication_definition in _MEDICATION_PATTERNS:
        for match in medication_definition.pattern.finditer(searchable):
            action = match.group("action").casefold()
            raw_medication = content[match.start("med") : match.end("med")]
            medication, unsupported = _canonical_medication(raw_medication)
            if medication in _NON_MEDICATION_ACTION_OBJECTS:
                continue
            facts.append(
                NormalizedFact(
                    fact_type="medication",
                    key=medication,
                    value=(
                        "stopped"
                        if action in medication_definition.stopped_actions
                        else "active"
                    ),
                    start=match.start(),
                    end=match.end(),
                    quote=content[match.start() : match.end()],
                    source_language=medication_definition.language,
                    review_required=unsupported,
                )
            )
    # Exact duplicate regex matches are harmless; retain a single span/value.
    return list(
        {
            (
                fact.fact_type,
                fact.key,
                fact.value,
                fact.assertion_scope,
                fact.allergy_category,
                fact.start,
                fact.end,
            ): fact
            for fact in facts
        }.values()
    )


def _pointer(
    session: Session,
    context: RequestContext,
    version: EntryVersion,
    content: str,
    fact: NormalizedFact,
) -> ProvenancePointer:
    pointer_id = uuid.uuid4()
    prefix = content[max(0, fact.start - 40) : fact.start]
    suffix = content[fact.end : fact.end + 40]
    pointer = ProvenancePointer(
        id=pointer_id,
        clinic_id=context.clinic_id,
        entry_version_id=version.id,
        start_offset=fact.start,
        end_offset=fact.end,
        exact_quote_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "provenance.exact_quote",
            pointer_id,
            fact.quote,
        ),
        prefix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.prefix", pointer_id, prefix
        ),
        suffix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.suffix", pointer_id, suffix
        ),
        quote_sha256=hashlib.sha256(fact.quote.encode()).hexdigest(),
    )
    session.add(pointer)
    session.flush()
    return pointer


def detect_conflicts_for_version(
    session: Session,
    context: RequestContext,
    entry: Entry,
    version: EntryVersion,
    content: str,
) -> tuple[list[ConflictCase], set[uuid.UUID]]:
    new_facts = extract_normalized_facts(content)
    # Only assertions bound to an entry's current immutable version participate
    # in active decision support. Keep older assertions addressable but mark
    # them superseded before materialising this replacement version.
    prior_assertions = session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            ClinicalFactAssertion.entry_id == entry.id,
            ClinicalFactAssertion.source_entry_version_id != version.id,
            ClinicalFactAssertion.assertion_state == "active",
        )
    ).all()
    superseded_at = datetime.now(UTC)
    stale_conflicts: list[ConflictCase] = []
    for prior in prior_assertions:
        prior.assertion_state = "superseded"
        prior.superseded_at = superseded_at
        session.add(prior)
    if prior_assertions:
        stale_conflicts = list(
            session.exec(
                select(ConflictCase).where(
                    ConflictCase.clinic_id == context.clinic_id,
                    ConflictCase.status == "unresolved",
                    col(ConflictCase.left_assertion_id).in_(
                        [item.id for item in prior_assertions]
                    )
                    | col(ConflictCase.right_assertion_id).in_(
                        [item.id for item in prior_assertions]
                    ),
                )
            ).all()
        )
        for conflict in stale_conflicts:
            conflict.status = "superseded"
            session.add(conflict)

    created: list[ConflictCase] = []
    new_assertions: list[tuple[NormalizedFact, ClinicalFactAssertion]] = []
    for fact in new_facts:
        pointer = _pointer(session, context, version, content, fact)
        assertion = create_assertion(
            session,
            clinic_id=context.clinic_id,
            patient_id=entry.patient_id,
            entry_id=entry.id,
            source_entry_version_id=version.id,
            provenance_pointer=pointer,
            fact_type=fact.fact_type,
            subject=fact.key,
            normalized_value=fact.value,
            origin=entry.origin,
            polarity=(fact.polarity if fact.fact_type == "allergy" else "present"),
            assertion_scope=fact.assertion_scope,
            source_language=fact.source_language,
            clinical_status=(
                "review_required"
                if fact.review_required
                else (fact.value if fact.fact_type == "medication" else "active")
            ),
            medication=(
                fact.key
                if fact.fact_type in {"medication", "dose", "route", "frequency"}
                else None
            ),
            dose_value=_dose_number(fact.value) if fact.fact_type == "dose" else None,
            dose_unit=(
                re.sub(r"[0-9.]", "", fact.value) if fact.fact_type == "dose" else None
            ),
            route=(fact.value if fact.fact_type == "route" else None),
            frequency=(fact.value if fact.fact_type == "frequency" else None),
        )
        new_assertions.append((fact, assertion))
        created.extend(detect_conflicts_for_assertion(session, context, assertion))

    # Preserve a direct replacement link for the same normalized fact when it
    # exists; assertions without a replacement remain historical on their own.
    for prior in prior_assertions:
        prior_subject = _assertion_text(prior, "subject").casefold()
        replacement = next(
            (
                assertion
                for fact, assertion in new_assertions
                if assertion.fact_type == prior.fact_type
                and fact.key.casefold() == prior_subject
            ),
            None,
        )
        if replacement is not None:
            prior.superseded_by_assertion_id = replacement.id
            session.add(prior)

    # Both a newly-created conflict and an edit-superseded conflict change the
    # linked highlight's safety protection. Recompute against the complete set
    # of still-active cases rather than toggling one flag from one transition.
    transitioned_conflicts = [*stale_conflicts, *created]
    assertion_ids = {
        assertion_id
        for conflict in transitioned_conflicts
        for assertion_id in (
            conflict.left_assertion_id,
            conflict.right_assertion_id,
        )
    }
    linked_highlight_ids: set[uuid.UUID] = set()
    if assertion_ids:
        linked_assertions = session.exec(
            select(ClinicalFactAssertion).where(
                ClinicalFactAssertion.clinic_id == context.clinic_id,
                col(ClinicalFactAssertion.id).in_(assertion_ids),
            )
        ).all()
        linked_highlight_ids = {
            assertion.highlight_id
            for assertion in linked_assertions
            if assertion.highlight_id is not None
        }
    session.flush()
    affected_patients = recompute_highlight_conflict_state(
        session,
        context,
        linked_highlight_ids,
    )
    return created, affected_patients


def _assertion_text(assertion: ClinicalFactAssertion, field: str) -> str:
    payload = (
        assertion.subject_ciphertext
        if field == "subject"
        else assertion.normalized_value_ciphertext
    )
    return field_codec.decrypt_text(
        assertion.clinic_id,
        f"fact_assertion.{field}",
        assertion.id,
        payload,
    )


def _allergy_assertions_conflict(
    left: ClinicalFactAssertion,
    left_subject: str,
    right: ClinicalFactAssertion,
    right_subject: str,
) -> bool:
    """Return whether two active allergy assertions have overlapping scope."""

    if left.polarity == "unknown" or right.polarity == "unknown":
        return False
    if left.polarity == right.polarity:
        return False
    scopes = {left.assertion_scope, right.assertion_scope}
    if scopes == {"specific_substance"}:
        return left_subject == right_subject
    if "all_allergies" in scopes:
        return True
    if scopes == {"drug_allergies"}:
        return True
    if "drug_allergies" in scopes:
        specific = left if left.assertion_scope == "specific_substance" else right
        # NKDA is drug-scoped.  Only an exact concept from the audited category
        # map can establish overlap; unknown, food, and environmental concepts
        # remain reviewable evidence but do not manufacture a drug conflict.
        return specific.allergy_category == "drug"
    return False


def _assertions_conflict(
    left: ClinicalFactAssertion,
    left_subject: str,
    left_value: str,
    right: ClinicalFactAssertion,
    right_subject: str,
    right_value: str,
) -> bool:
    if left.fact_type == "allergy":
        return _allergy_assertions_conflict(left, left_subject, right, right_subject)
    return left_subject == right_subject and left_value != right_value


def detect_conflicts_for_assertion(
    session: Session,
    context: RequestContext,
    assertion: ClinicalFactAssertion,
) -> list[ConflictCase]:
    """Compare a newly persisted Human/AI/Voice assertion across all origins."""

    subject = _assertion_text(assertion, "subject").casefold()
    value = _assertion_text(assertion, "normalized_value").casefold()
    created: list[ConflictCase] = []
    candidates = session.exec(
        select(ClinicalFactAssertion)
        .join(Entry, col(Entry.id) == col(ClinicalFactAssertion.entry_id))
        .where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            ClinicalFactAssertion.patient_id == assertion.patient_id,
            ClinicalFactAssertion.fact_type == assertion.fact_type,
            ClinicalFactAssertion.id != assertion.id,
            ClinicalFactAssertion.assertion_state == "active",
            Entry.clinic_id == context.clinic_id,
            Entry.current_version_id == ClinicalFactAssertion.source_entry_version_id,
        )
    ).all()
    for other in candidates:
        other_subject = _assertion_text(other, "subject").casefold()
        other_value = _assertion_text(other, "normalized_value").casefold()
        if not _assertions_conflict(
            other,
            other_subject,
            other_value,
            assertion,
            subject,
            value,
        ):
            continue
        left, right = sorted((other, assertion), key=lambda item: str(item.id))
        duplicate = session.exec(
            select(ConflictCase).where(
                ConflictCase.clinic_id == context.clinic_id,
                ConflictCase.patient_id == assertion.patient_id,
                ConflictCase.fact_type == assertion.fact_type,
                ConflictCase.left_assertion_id == left.id,
                ConflictCase.right_assertion_id == right.id,
                ConflictCase.status == "unresolved",
            )
        ).first()
        if duplicate is not None:
            continue
        normalized_key = (
            subject
            if assertion.assertion_scope == "specific_substance"
            else other_subject
        )
        if normalized_key.startswith("*"):
            normalized_key = "allergy"
        conflict = ConflictCase(
            clinic_id=context.clinic_id,
            patient_id=assertion.patient_id,
            left_entry_id=left.entry_id,
            right_entry_id=right.entry_id,
            fact_type=assertion.fact_type,
            normalized_key=normalized_key,
            left_version_id=left.source_entry_version_id,
            right_version_id=right.source_entry_version_id,
            left_pointer_id=left.provenance_pointer_id,
            right_pointer_id=right.provenance_pointer_id,
            left_assertion_id=left.id,
            right_assertion_id=right.id,
            severity="critical" if assertion.fact_type == "allergy" else "high",
            status="unresolved",
        )
        session.add(conflict)
        created.append(conflict)
        for candidate in (left, right):
            if candidate.highlight_id is None:
                continue
            highlight = session.get(Highlight, candidate.highlight_id)
            if highlight is None or highlight.clinic_id != context.clinic_id:
                continue
            # Critical, label, and source fields are immutable parts of the
            # highlight anchor. The linked ConflictCase carries allergy
            # severity; the mutable unresolved flag places the item in the
            # independent safety-review queue without rewriting its source.
            highlight.unresolved = True
            session.add(highlight)
    return created


def recompute_highlight_conflict_state(
    session: Session,
    context: RequestContext,
    highlight_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    """Recompute conflict-derived protection after a clinician resolves a case.

    A highlight may participate in more than one conflict, so resolving one
    case must not blindly clear its safety state. Conversely, conflict-derived
    `critical`/`risk:critical` flags must not keep a resolved item permanently
    trapped in the uncapped review queue. Only active assertions bound to the
    owning entry's current immutable version count.
    """

    affected_patients: set[uuid.UUID] = set()
    for highlight_id in highlight_ids:
        highlight = session.exec(
            select(Highlight)
            .where(
                Highlight.id == highlight_id,
                Highlight.clinic_id == context.clinic_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if highlight is None:
            continue
        assertion_ids = list(
            session.exec(
                select(ClinicalFactAssertion.id)
                .join(Entry, col(Entry.id) == col(ClinicalFactAssertion.entry_id))
                .where(
                    ClinicalFactAssertion.clinic_id == context.clinic_id,
                    ClinicalFactAssertion.highlight_id == highlight.id,
                    ClinicalFactAssertion.assertion_state == "active",
                    Entry.clinic_id == context.clinic_id,
                    Entry.current_version_id
                    == ClinicalFactAssertion.source_entry_version_id,
                )
            ).all()
        )
        remaining = (
            session.exec(
                select(ConflictCase).where(
                    ConflictCase.clinic_id == context.clinic_id,
                    ConflictCase.patient_id == highlight.patient_id,
                    ConflictCase.status == "unresolved",
                    col(ConflictCase.left_assertion_id).in_(assertion_ids)
                    | col(ConflictCase.right_assertion_id).in_(assertion_ids),
                )
            ).all()
            if assertion_ids
            else []
        )
        # Conflict severity lives on immutable, source-linked ConflictCase
        # records. Only this mutable projection flag is changed here; the
        # anchor's critical/feature fields remain frozen by the database guard.
        highlight.unresolved = bool(remaining)
        session.add(highlight)
        affected_patients.add(highlight.patient_id)
    return affected_patients
