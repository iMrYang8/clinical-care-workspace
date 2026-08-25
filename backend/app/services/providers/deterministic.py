from __future__ import annotations

import re

from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
)


class DeterministicClinicalNoteProvider:
    """Small local extractor used by offline CI and explicit fallback states."""

    provider_name = "deterministic"
    model_name = "nightingale-rules-v1"

    def __init__(self, *, review_model_configured: bool = True) -> None:
        self.review_model_configured = review_model_configured

    async def extract(
        self, redacted_text: str, context: ExtractionContext
    ) -> ClinicalNoteDraft:
        facts: list[ClinicalFact] = []
        for match in re.finditer(r"[^.!?\n]+[.!?]?", redacted_text):
            sentence = match.group(0)
            lowered = sentence.lower()
            stripped = sentence.strip()
            if not stripped:
                continue
            leading = len(sentence) - len(sentence.lstrip())
            start = match.start() + leading
            end = start + len(stripped)
            if "allerg" in lowered:
                facts.append(
                    ClinicalFact(
                        fact_type="allergy",
                        value=stripped,
                        evidence_start=start,
                        evidence_end=end,
                        evidence_quote=redacted_text[start:end],
                        feature_keys=["entity:allergy"],
                        critical="critical" in lowered or "anaphyl" in lowered,
                    )
                )
            if "follow up" in lowered or "follow-up" in lowered:
                facts.append(
                    ClinicalFact(
                        fact_type="follow_up",
                        value=stripped,
                        evidence_start=start,
                        evidence_end=end,
                        evidence_quote=redacted_text[start:end],
                        feature_keys=["topic:follow_up"],
                    )
                )

        warnings: list[str] = []
        needs_review = False
        if (
            context.high_risk or context.conflict_review
        ) and not self.review_model_configured:
            warnings.append("HIGH_RISK_REVIEW_MODEL_UNAVAILABLE")
            needs_review = True
        if not facts:
            warnings.append("NO_STRUCTURED_FACTS")
            needs_review = True
        return ClinicalNoteDraft(
            summary=" ".join(redacted_text.split())[:2_000],
            facts=facts,
            provider=self.provider_name,
            model=self.model_name,
            warnings=warnings,
            needs_review=needs_review,
        )
