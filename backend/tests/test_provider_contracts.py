import asyncio
import hashlib
import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.services.egress import QualifiedRedactedText
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


def _qualified(text: str) -> QualifiedRedactedText:
    digest = hashlib.sha256(text.encode()).hexdigest()

    class Report:
        redacted_text = text
        redacted_sha256 = digest
        residual_scan_passed = True
        remote_egress_allowed = True
        status = "complete"
        error_code = None

    return QualifiedRedactedText.from_report(Report())


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
        provider.extract(_qualified("[KNOWN_NAME_1] allergy"), _context(high_risk=True))
    )
    review = asyncio.run(
        provider.review(
            _qualified("[KNOWN_NAME_1] allergy"),
            _context(high_risk=True),
            primary,
        )
    )

    assert calls[0] == ("configured-extract-id", "[KNOWN_NAME_1] allergy")
    assert calls[1][0] == "configured-review-id"
    assert "[KNOWN_NAME_1] allergy" in calls[1][1]
    assert primary.model == "configured-extract-id"
    assert review.model == "configured-review-id"


def test_openai_responses_envelope_text_is_parsed_without_sdk_shape_assumptions() -> (
    None
):
    assert _response_text({"output_text": '{"summary":"direct"}'}) == (
        '{"summary":"direct"}'
    )
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

    for malformed in (
        {"output": "not-a-list"},
        {"output": ["not-an-object"]},
        {"output": [{"content": "not-a-list"}]},
    ):
        with pytest.raises(ValueError, match="PROVIDER_INVALID_RESPONSE"):
            _response_text(malformed)


def test_openai_warning_text_is_never_reflected_from_provider() -> None:
    draft = OpenAITextProvider._draft_from_raw(
        {
            "summary": "safe",
            "facts": [],
            "warnings": ["S1234567D", "TAN_MEI_LING"],
        },
        model="configured-model",
    )
    assert draft.warnings == ["PROVIDER_REPORTED_WARNING"]


def test_openai_provider_rejects_missing_configuration_and_review_model() -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        OpenAITextProvider(api_key="", extract_model="configured-model")
    with pytest.raises(ValueError, match="requires an API key"):
        OpenAITextProvider(api_key="TOKEN", extract_model="")

    provider = OpenAITextProvider(
        api_key="TOKEN",
        extract_model="configured-model",
        transport=lambda *_args: asyncio.sleep(0, result={}),
    )
    primary = ClinicalNoteDraft(
        summary="review fixture",
        facts=[],
        provider="fixture",
        model="fixture",
    )
    with pytest.raises(ValueError, match="HIGH_RISK_REVIEW_MODEL_UNAVAILABLE"):
        asyncio.run(provider.review(_qualified("safe"), _context(), primary))


def test_openai_draft_parser_fails_closed_on_malformed_fact_shapes() -> None:
    invalid_collection = OpenAITextProvider._draft_from_raw(
        {"summary": "safe", "facts": "not-a-list", "warnings": "invalid"},
        model="configured-model",
    )
    assert invalid_collection.facts == []
    assert invalid_collection.needs_review is True
    assert invalid_collection.warnings == [
        "PROVIDER_WARNING_SCHEMA_INVALID",
        "PROVIDER_FACT_SCHEMA_INVALID",
    ]

    source = "unique penicillin evidence"
    raw_facts: list[object] = [
        "not-an-object",
        {"fact_type": "missing-fields"},
        {
            "fact_type": "allergy",
            "value": "penicillin",
            "evidence_start": 0,
            "evidence_end": 1,
            "evidence_quote": "penicillin",
            "feature_keys": ["entity:allergy"],
            "critical": True,
        },
    ]
    # More than 50 candidates is itself review-required; the parser remains
    # bounded and never reflects arbitrary warning strings.
    raw_facts.extend({"fact_type": "missing-fields"} for _ in range(50))
    repaired = OpenAITextProvider._draft_from_raw(
        {
            "summary": "bounded",
            "facts": raw_facts,
            "warnings": [],
            "needs_review": False,
        },
        model="configured-model",
        source_text=source,
    )
    assert len(repaired.facts) == 1
    assert repaired.facts[0].evidence_start == source.index("penicillin")
    assert repaired.facts[0].evidence_end == source.index("penicillin") + len(
        "penicillin"
    )
    assert repaired.facts[0].critical is True
    assert repaired.needs_review is True
    assert repaired.warnings == ["PROVIDER_FACT_SCHEMA_INVALID"]


def test_openai_http_transport_uses_bounded_responses_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {
                "output_text": json.dumps(
                    {
                        "summary": "configured",
                        "facts": [],
                        "warnings": [],
                        "needs_review": False,
                    }
                )
            }

    class AsyncClient:
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            observed.append({"timeout": timeout})

        async def __aenter__(self) -> "AsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> Response:
            observed[-1].update({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr(
        "app.services.providers.openai_text.httpx.AsyncClient", AsyncClient
    )
    provider = OpenAITextProvider(
        api_key="TOKEN",
        extract_model="extract-model",
        review_model="review-model",
        timeout_seconds=11,
        connect_timeout_seconds=3,
    )
    primary = asyncio.run(provider.extract(_qualified("redacted text"), _context()))
    reviewed = asyncio.run(
        provider.review(_qualified("redacted text"), _context(), primary)
    )

    assert primary.model == "extract-model"
    assert reviewed.model == "review-model"
    assert len(observed) == 2
    assert observed[0]["url"] == "https://api.openai.com/v1/responses"
    assert observed[0]["headers"] == {"Authorization": "Bearer TOKEN"}
    assert observed[0]["json"]["text"] == {"format": {"type": "json_object"}}
    assert observed[0]["timeout"].connect == 3
    assert observed[0]["timeout"].read == 11
    assert "Independently review" in observed[1]["json"]["input"][0]["content"]


def test_openai_http_transport_rejects_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"output_text": "[]"}

    class AsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "AsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(
        "app.services.providers.openai_text.httpx.AsyncClient", AsyncClient
    )
    provider = OpenAITextProvider(api_key="TOKEN", extract_model="extract-model")
    with pytest.raises(ValueError, match="PROVIDER_INVALID_RESPONSE"):
        asyncio.run(provider.extract(_qualified("redacted text"), _context()))


def test_worker_control_codes_and_loop_entrypoints_are_bounded(monkeypatch) -> None:
    import app.ai_worker as ai_worker

    assert (
        ai_worker._safe_http_code(
            HTTPException(status_code=409, detail={"code": "JOB_CLAIM_LOST"})
        )
        == "JOB_CLAIM_LOST"
    )
    assert (
        ai_worker._safe_http_code(HTTPException(status_code=500, detail="S1234567D"))
        == "JOB_PROCESSING_REJECTED"
    )

    async def stop() -> int:
        raise RuntimeError("STOP_LOOP")

    monkeypatch.setattr(ai_worker, "run_once", stop)
    with pytest.raises(RuntimeError, match="STOP_LOOP"):
        asyncio.run(ai_worker.run_forever())

    async def complete() -> None:
        return None

    monkeypatch.setattr(ai_worker, "run_forever", complete)
    ai_worker.main()
