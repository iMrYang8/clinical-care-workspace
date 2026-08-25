import asyncio
import uuid

import pytest

from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
    validate_evidence,
)
from app.services.providers.deterministic import DeterministicClinicalNoteProvider
from app.services.providers.openai_text import OpenAITextProvider

pytestmark = pytest.mark.unit


def _context(*, high_risk: bool = False) -> ExtractionContext:
    return ExtractionContext(
        clinic_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        high_risk=high_risk,
    )


def test_deterministic_provider_emits_verifiable_evidence_spans() -> None:
    source = "The patient reports allergy to penicillin. Follow up in two weeks."
    draft = asyncio.run(DeterministicClinicalNoteProvider().extract(source, _context()))

    checked = validate_evidence(draft, source)

    assert checked.facts
    assert checked.needs_review is False
    for fact in checked.facts:
        assert source[fact.evidence_start : fact.evidence_end] == fact.evidence_quote


def test_invalid_evidence_is_discarded_and_forces_review() -> None:
    source = "No matching evidence is present."
    draft = ClinicalNoteDraft(
        summary="unsupported",
        facts=[
            ClinicalFact(
                fact_type="allergy",
                value="penicillin",
                evidence_start=0,
                evidence_end=5,
                evidence_quote="wrong",
                feature_keys=["entity:allergy"],
            )
        ],
        provider="remote",
        model="configured-model",
    )

    checked = validate_evidence(draft, source)

    assert checked.facts == []
    assert checked.needs_review is True
    assert "INVALID_EVIDENCE_SPAN" in checked.warnings


def test_high_risk_without_review_model_is_explicitly_review_required() -> None:
    source = "Critical allergy to penicillin."
    provider = DeterministicClinicalNoteProvider(review_model_configured=False)

    draft = asyncio.run(provider.extract(source, _context(high_risk=True)))

    assert draft.needs_review is True
    assert "HIGH_RISK_REVIEW_MODEL_UNAVAILABLE" in draft.warnings


def test_openai_boundary_uses_only_configured_model_ids_and_review_route() -> None:
    calls: list[tuple[str, str]] = []

    async def transport(model: str, text: str, _interaction_type: str) -> dict:
        calls.append((model, text))
        return {
            "summary": "configured",
            "facts": [],
            "warnings": [],
            "needs_review": False,
        }

    provider = OpenAITextProvider(
        api_key="synthetic-test-key",
        extract_model="configured-extract-id",
        review_model="configured-review-id",
        transport=transport,
    )
    draft = asyncio.run(
        provider.extract("[KNOWN_NAME_1] allergy", _context(high_risk=True))
    )

    assert calls == [("configured-review-id", "[KNOWN_NAME_1] allergy")]
    assert draft.model == "configured-review-id"
