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
from app.services.providers.openai_text import OpenAITextProvider, _response_text

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


def test_openai_boundary_runs_primary_then_configured_review_model() -> None:
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
    primary = asyncio.run(
        provider.extract("[KNOWN_NAME_1] allergy", _context(high_risk=True))
    )
    review = asyncio.run(
        provider.review("[KNOWN_NAME_1] allergy", _context(high_risk=True), primary)
    )

    assert calls[0] == ("configured-extract-id", "[KNOWN_NAME_1] allergy")
    assert calls[1][0] == "configured-review-id"
    assert "[KNOWN_NAME_1] allergy" in calls[1][1]
    assert primary.model == "configured-extract-id"
    assert review.model == "configured-review-id"


def test_openai_responses_envelope_text_is_parsed_without_sdk_shape_assumptions() -> (
    None
):
    assert (
        _response_text(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"facts": []}'}],
                    }
                ]
            }
        )
        == '{"facts": []}'
    )
    with pytest.raises(ValueError, match="PROVIDER_INVALID_RESPONSE"):
        _response_text({"output": [{"content": [{"type": "refusal"}]}]})
