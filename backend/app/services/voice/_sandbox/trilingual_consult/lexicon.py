"""Sandbox lexicon sized for NightingaleSwitchCare gold.

Inspired by Nightingale voice conflict extraction and clinic formulary
modules as of 2026-09-02. This is a snapshot, not a live import — Phase A
must not couple to the moving main tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trilingual_consult.state import LanguageCode, LanguageSpan

DRUG_ALIASES: dict[str, frozenset[str]] = {
    "penicillin": frozenset(
        {"penicillin", "penisilin", "盘尼西林", "青霉素", "青黴素"}
    ),
    "amoxicillin": frozenset({"amoxicillin", "amoksisilin", "阿莫西林"}),
    "metformin": frozenset({"metformin", "二甲双胍", "二甲雙胍"}),
    "aspirin": frozenset({"aspirin", "asipirin", "阿司匹林"}),
}

PENICILLIN_CLASS = frozenset({"penicillin", "amoxicillin"})

DOSE_RANGE_MG: dict[str, tuple[float, float]] = {
    "metformin": (250.0, 1_000.0),
    "amoxicillin": (125.0, 1_000.0),
    "aspirin": (50.0, 1_000.0),
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in DRUG_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias.casefold()] = _canonical

_EN_ISLAND = re.compile(
    r"\b(?:penicillin|penisilin|amoxicillin|amoksisilin|metformin|aspirin)\b",
    re.I,
)
_TAG = re.compile(r"\[\[([A-Z]+)\]\](.*?)\[\[/\1\]\]", re.S)

_LANGUAGE_MARKERS: tuple[tuple[re.Pattern[str], LanguageCode], ...] = (
    (
        re.compile(
            r"\b(?:alahan|alergi|tiada|kepada|terhadap|pesakit|dia|masa|"
            r"kecil|ada|mula|teruskan|sehari|kali)\b",
            re.I,
        ),
        "ms",
    ),
    (
        re.compile(
            r"\b(?:koe-bin|koe bin|tui|tùi|bo|chiah|khau-hok)\b",
            re.I,
        ),
        "nan",
    ),
    (re.compile(r"[\u3400-\u9fff]+"), "zh"),
    (
        re.compile(
            r"\b(?:allergic|allergy|continue|we'll|we will|twice|daily|"
            r"started|taking)\b",
            re.I,
        ),
        "en",
    ),
)


@dataclass(frozen=True)
class AllergyMatch:
    start: int
    end: int
    quote: str
    key: str
    polarity: str
    language: LanguageCode
    penicillin_class: bool


@dataclass(frozen=True)
class MedicationMatch:
    start: int
    end: int
    quote: str
    key: str
    fact_type: str
    value: str
    language: LanguageCode


def canonicalize_drug(raw: str) -> str | None:
    return _ALIAS_TO_CANONICAL.get(raw.strip().casefold())


def strip_language_tags(tagged: str) -> str:
    return _TAG.sub(lambda match: match.group(2), tagged)


def english_islands_from_tags(tagged: str, text: str) -> list[tuple[int, int, str]]:
    """Return offsets of [[EN]] islands inside the working (untagged) text."""

    islands: list[tuple[int, int, str]] = []
    search_from = 0
    for match in _TAG.finditer(tagged):
        if match.group(1) != "EN":
            continue
        inner = match.group(2).strip()
        if not inner:
            continue
        start = text.find(inner, search_from)
        if start < 0:
            start = text.find(inner)
        if start < 0:
            continue
        end = start + len(inner)
        islands.append((start, end, inner))
        search_from = end
    return islands


def normalize_language(value: str | None) -> LanguageCode:
    if not value:
        return "und"
    primary = value.strip().lower().replace("_", "-").split("-", 1)[0]
    if primary in {"en", "ms", "nan", "zh"}:
        return primary  # type: ignore[return-value]
    if primary in {"cmn", "zh"}:
        return "zh"
    return "und"


def detect_language_spans(
    text: str,
    *,
    turn_index: int,
    source_language: str | None = None,
    tagged_text: str | None = None,
) -> list[LanguageSpan]:
    """Gap-free spans. English drug islands split a matrix-language turn."""

    if not text:
        return []
    fallback = normalize_language(source_language)
    labels: list[LanguageCode] = [fallback] * len(text)
    for pattern, language in _LANGUAGE_MARKERS:
        for match in pattern.finditer(text):
            labels[match.start() : match.end()] = [language] * (match.end() - match.start())
    for match in _EN_ISLAND.finditer(text):
        labels[match.start() : match.end()] = ["en"] * (match.end() - match.start())
    if tagged_text:
        for start, end, _inner in english_islands_from_tags(tagged_text, text):
            end = min(end, len(text))
            if start < end:
                labels[start:end] = ["en"] * (end - start)
    spans: list[LanguageSpan] = []
    cursor = 0
    for index in range(1, len(text) + 1):
        if index == len(text) or labels[index] != labels[cursor]:
            spans.append(
                LanguageSpan(
                    start=cursor,
                    end=index,
                    language=labels[cursor],
                    turn_index=turn_index,
                    review_required=labels[cursor] == "und",
                )
            )
            cursor = index
    return spans


_ALLERGY_PATTERNS: tuple[tuple[re.Pattern[str], LanguageCode, str, int], ...] = (
    (
        re.compile(r"\bno(?:t)? allergic to\s+([A-Za-z][A-Za-z0-9-]*)", re.I),
        "en",
        "absent",
        1,
    ),
    (
        re.compile(r"\b(?:allergic to|allergy to)\s+([A-Za-z][A-Za-z0-9-]*)", re.I),
        "en",
        "present",
        1,
    ),
    (
        re.compile(
            r"\b(?:tiada)\s+(?:alahan|alergi)\s+(?:kepada|terhadap)\s+"
            r"([A-Za-z][A-Za-z0-9-]*)",
            re.I,
        ),
        "ms",
        "absent",
        1,
    ),
    (
        re.compile(
            r"\b(?:alahan|alergi)\s+(?:kepada|terhadap)\s+([A-Za-z][A-Za-z0-9-]*)",
            re.I,
        ),
        "ms",
        "present",
        1,
    ),
    (
        re.compile(r"(?:我)?对\s*(盘尼西林|青霉素|青黴素|penicillin)\s*不过敏"),
        "zh",
        "absent",
        1,
    ),
    (
        re.compile(r"(?:我)?对\s*(盘尼西林|青霉素|青黴素|penicillin)\s*过敏"),
        "zh",
        "present",
        1,
    ),
    (
        re.compile(
            r"\bbo\s+(?:tui|ti)\s+([A-Za-z][A-Za-z0-9-]*)\s+(?:koe-bin|koe bin)\b",
            re.I,
        ),
        "nan",
        "absent",
        1,
    ),
    (
        re.compile(
            r"\b(?:tui|tùi|ti)\s+([A-Za-z][A-Za-z0-9-]*)\s+(?:koe-bin|koe bin)\b",
            re.I,
        ),
        "nan",
        "present",
        1,
    ),
)

_ALLERGY_KEYWORDS: tuple[tuple[re.Pattern[str], LanguageCode], ...] = (
    (re.compile(r"\ballerg(?:y|ies|ic)\b", re.I), "en"),
    (re.compile(r"\b(?:alahan|alergi)\b", re.I), "ms"),
    (re.compile(r"\b(?:koe-bin|koe bin)\b", re.I), "nan"),
    (re.compile(r"(?:过敏|過敏)"), "zh"),
)

_DOSE = re.compile(
    r"\b(?P<med>metformin|amoxicillin|amoksisilin|aspirin|penicillin)\s+"
    r"(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g)\b",
    re.I,
)
_FREQUENCY = re.compile(
    r"\b(twice daily|dua kali sehari|每日两次|每日兩次|once daily|sekali sehari)\b",
    re.I,
)


def extract_allergy_matches(text: str) -> list[AllergyMatch]:
    occupied: list[tuple[int, int]] = []
    matches: list[AllergyMatch] = []
    for pattern, language, polarity, group in _ALLERGY_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() >= start and match.end() <= end for start, end in occupied):
                continue
            raw = match.group(group)
            key = canonicalize_drug(raw) or raw.strip().casefold()
            matches.append(
                AllergyMatch(
                    start=match.start(),
                    end=match.end(),
                    quote=text[match.start() : match.end()],
                    key=key,
                    polarity=polarity,
                    language=language,
                    penicillin_class=key in PENICILLIN_CLASS,
                )
            )
            occupied.append((match.start(), match.end()))
    for pattern, language in _ALLERGY_KEYWORDS:
        for match in pattern.finditer(text):
            if any(match.start() >= start and match.end() <= end for start, end in occupied):
                continue
            matches.append(
                AllergyMatch(
                    start=match.start(),
                    end=match.end(),
                    quote=text[match.start() : match.end()],
                    key="*",
                    polarity="unknown",
                    language=language,
                    penicillin_class=False,
                )
            )
            occupied.append((match.start(), match.end()))
    return matches


def extract_medication_matches(text: str) -> list[MedicationMatch]:
    matches: list[MedicationMatch] = []
    occupied: list[tuple[int, int]] = []
    for match in _DOSE.finditer(text):
        key = canonicalize_drug(match.group("med"))
        if key is None:
            continue
        unit = match.group("unit").lower()
        dose = float(match.group("dose"))
        if unit == "g":
            dose *= 1_000
            unit = "mg"
        elif unit == "mcg":
            dose /= 1_000
            unit = "mg"
        quote = match.group(0)
        matches.append(
            MedicationMatch(
                start=match.start(),
                end=match.end(),
                quote=quote,
                key=key,
                fact_type="medication",
                value=key,
                language="en",
            )
        )
        matches.append(
            MedicationMatch(
                start=match.start(),
                end=match.end(),
                quote=quote,
                key=key,
                fact_type="dose",
                value=f"{dose:g}{unit}",
                language="en",
            )
        )
        occupied.append((match.start(), match.end()))
    for match in _FREQUENCY.finditer(text):
        raw = match.group(1).casefold()
        value = (
            "twice_daily"
            if "twice" in raw or "dua" in raw or "两" in raw or "兩" in raw
            else "once_daily"
        )
        med_key = "metformin"
        for previous in matches:
            if previous.fact_type == "medication":
                med_key = previous.key
                break
        matches.append(
            MedicationMatch(
                start=match.start(),
                end=match.end(),
                quote=match.group(0),
                key=med_key,
                fact_type="frequency",
                value=value,
                language="en" if "daily" in raw or "twice" in raw else "ms",
            )
        )
    return matches


def has_hokkien_cue(text: str) -> bool:
    if re.search(r"\b(?:koe-bin|koe bin|tui|tùi|bo)\b", text, re.I):
        return True
    if re.search(r"(?:过敏|過敏|盘尼西林|青霉素)", text):
        return True
    return False


def count_cmi_and_switches(turns_tagged: list[str]) -> tuple[float, int]:
    """Das & Gambäck style: fraction of tokens whose language ≠ utterance majority."""

    total_tokens = 0
    mixed_tokens = 0
    switch_points = 0
    for tagged in turns_tagged:
        if not tagged:
            continue
        pieces: list[tuple[str, str]] = []
        cursor = 0
        for match in _TAG.finditer(tagged):
            if match.start() > cursor:
                pieces.append(("matrix", tagged[cursor : match.start()]))
            pieces.append((match.group(1).lower(), match.group(2)))
            cursor = match.end()
        if cursor < len(tagged):
            pieces.append(("matrix", tagged[cursor:]))
        tokens: list[str] = []
        langs: list[str] = []
        for language, chunk in pieces:
            for token in re.findall(r"[A-Za-z0-9\u3400-\u9fff]+", chunk):
                tokens.append(token)
                langs.append("en" if language == "en" else "matrix")
        if not tokens:
            continue
        majority = "matrix"
        en_count = sum(1 for item in langs if item == "en")
        if en_count > len(tokens) / 2:
            majority = "en"
        total_tokens += len(tokens)
        mixed_tokens += sum(1 for item in langs if item != majority)
        switch_points += sum(
            1 for left, right in zip(langs, langs[1:], strict=False) if left != right
        )
    if total_tokens == 0:
        return 0.0, 0
    return round(mixed_tokens / total_tokens, 4), switch_points
