from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Protocol


@dataclass(frozen=True)
class ExtractionContext:
    clinic_id: uuid.UUID
    patient_id: uuid.UUID
    source_version_id: uuid.UUID
    interaction_type: str = "care_note"
    high_risk: bool = False
    conflict_review: bool = False


@dataclass(frozen=True)
class ClinicalFact:
    fact_type: str
    value: str
    evidence_start: int
    evidence_end: int
    evidence_quote: str
    feature_keys: list[str] = field(default_factory=list)
    critical: bool = False


@dataclass(frozen=True)
class ClinicalNoteDraft:
    summary: str
    facts: list[ClinicalFact]
    provider: str
    model: str
    warnings: list[str] = field(default_factory=list)
    needs_review: bool = False


class ClinicalNoteProvider(Protocol):
    async def extract(
        self, redacted_text: str, context: ExtractionContext
    ) -> ClinicalNoteDraft: ...


class ClinicalReviewProvider(ClinicalNoteProvider, Protocol):
    review_model: str | None

    async def review(
        self,
        redacted_text: str,
        context: ExtractionContext,
        primary: ClinicalNoteDraft,
    ) -> ClinicalNoteDraft: ...


def validate_evidence(draft: ClinicalNoteDraft, source_text: str) -> ClinicalNoteDraft:
    """Discard unsupported facts instead of guessing a source anchor."""

    valid: list[ClinicalFact] = []
    invalid = False
    for fact in draft.facts:
        if (
            0 <= fact.evidence_start <= fact.evidence_end <= len(source_text)
            and source_text[fact.evidence_start : fact.evidence_end]
            == fact.evidence_quote
            and bool(fact.evidence_quote)
        ):
            valid.append(fact)
        else:
            invalid = True
    warnings = list(draft.warnings)
    if invalid and "INVALID_EVIDENCE_SPAN" not in warnings:
        warnings.append("INVALID_EVIDENCE_SPAN")
    return replace(
        draft,
        facts=valid,
        warnings=warnings,
        needs_review=draft.needs_review or invalid,
    )
