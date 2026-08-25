from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
)

Transport = Callable[[str, str, str], Awaitable[dict[str, Any]]]


class OpenAITextProvider:
    """Config-only OpenAI boundary; model identifiers are never hard-coded."""

    def __init__(
        self,
        *,
        api_key: str,
        extract_model: str,
        review_model: str | None = None,
        transport: Transport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key or not extract_model:
            raise ValueError("OpenAI provider requires an API key and configured model")
        self.api_key = api_key
        self.extract_model = extract_model
        self.review_model = review_model
        self.transport = transport or self._http_transport
        self.timeout_seconds = timeout_seconds

    async def _http_transport(
        self, model: str, text: str, interaction_type: str
    ) -> dict[str, Any]:
        schema_prompt = (
            "Return JSON with summary, facts, warnings, needs_review. "
            "Every fact must include fact_type, value, evidence_start, "
            "evidence_end, evidence_quote, feature_keys, critical."
        )
        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": schema_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"interaction_type": interaction_type, "text": text}
                    ),
                },
            ],
            "text": {"format": {"type": "json_object"}},
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        output_text = body.get("output_text")
        if not isinstance(output_text, str):
            raise ValueError("PROVIDER_INVALID_RESPONSE")
        parsed = json.loads(output_text)
        if not isinstance(parsed, dict):
            raise ValueError("PROVIDER_INVALID_RESPONSE")
        return parsed

    async def extract(
        self, redacted_text: str, context: ExtractionContext
    ) -> ClinicalNoteDraft:
        needs_second_review = context.high_risk or context.conflict_review
        model = (
            self.review_model
            if needs_second_review and self.review_model
            else self.extract_model
        )
        raw = await self.transport(model, redacted_text, context.interaction_type)
        facts: list[ClinicalFact] = []
        raw_facts = raw.get("facts", [])
        invalid_fact = not isinstance(raw_facts, list)
        if isinstance(raw_facts, list) and len(raw_facts) > 50:
            invalid_fact = True
        for item in raw_facts[:50] if isinstance(raw_facts, list) else []:
            if not isinstance(item, dict):
                invalid_fact = True
                continue
            try:
                facts.append(
                    ClinicalFact(
                        fact_type=str(item["fact_type"])[:80],
                        value=str(item["value"])[:2_000],
                        evidence_start=int(item["evidence_start"]),
                        evidence_end=int(item["evidence_end"]),
                        evidence_quote=str(item["evidence_quote"])[:20_000],
                        feature_keys=[
                            str(key)[:120] for key in item.get("feature_keys", [])[:20]
                        ],
                        critical=bool(item.get("critical", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                invalid_fact = True
                continue
        raw_warnings = raw.get("warnings", [])
        warnings = (
            [str(item) for item in raw_warnings]
            if isinstance(raw_warnings, list)
            else ["PROVIDER_WARNING_SCHEMA_INVALID"]
        )
        needs_review = bool(raw.get("needs_review", False))
        if invalid_fact:
            warnings.append("PROVIDER_FACT_SCHEMA_INVALID")
            needs_review = True
        if needs_second_review and not self.review_model:
            warnings.append("HIGH_RISK_REVIEW_MODEL_UNAVAILABLE")
            needs_review = True
        return ClinicalNoteDraft(
            summary=str(raw.get("summary", ""))[:20_000],
            facts=facts,
            provider="openai",
            model=model,
            warnings=warnings,
            needs_review=needs_review,
        )
