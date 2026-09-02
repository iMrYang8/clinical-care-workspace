from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.services.egress import QualifiedRedactedText
from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
)

Transport = Callable[[str, str, str], Awaitable[dict[str, Any]]]


def _response_text(body: dict[str, Any]) -> str:
    direct = body.get("output_text")
    if isinstance(direct, str):
        return direct
    chunks: list[str] = []
    output = body.get("output", [])
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("content", [])
        for part in content if isinstance(content, list) else []:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                chunks.append(part["text"])
    if not chunks:
        raise ValueError("PROVIDER_INVALID_RESPONSE")
    return "".join(chunks)


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
        connect_timeout_seconds: float = 5.0,
    ) -> None:
        if not api_key or not extract_model:
            raise ValueError("OpenAI provider requires an API key and configured model")
        self.api_key = api_key
        self.extract_model = extract_model
        self.review_model = review_model
        # Raw-string transport is private to this provider. Production callers
        # must cross the QualifiedRedactedText methods guarded below.
        self._transport = transport or self._http_transport
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds

    async def _http_transport(
        self, model: str, text: str, interaction_type: str
    ) -> dict[str, Any]:
        schema_prompt = (
            "Independently review the primary extraction against redacted_source; "
            "all evidence offsets must index redacted_source, not the JSON wrapper. "
            if interaction_type.endswith(":review")
            else "Extract a clinical note from the redacted source. "
        ) + (
            "Return JSON with summary, facts, warnings, needs_review. Every fact "
            "must include fact_type, value, evidence_start, evidence_end, "
            "evidence_quote, feature_keys, critical."
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
        timeout = httpx.Timeout(
            self.timeout_seconds, connect=self.connect_timeout_seconds
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        parsed = json.loads(_response_text(body))
        if not isinstance(parsed, dict):
            raise ValueError("PROVIDER_INVALID_RESPONSE")
        return parsed

    @staticmethod
    def _draft_from_raw(
        raw: dict[str, Any],
        *,
        model: str,
        source_text: str | None = None,
    ) -> ClinicalNoteDraft:
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
                evidence_quote = str(item["evidence_quote"])[:20_000]
                evidence_start = int(item["evidence_start"])
                evidence_end = int(item["evidence_end"])
                # Models are poor character counters.  Repair an offset only
                # when the verbatim quote has one unambiguous occurrence in
                # the primary source.  Repeated or absent quotes remain
                # invalid and are discarded by validate_evidence rather than
                # attaching a fact to a guessed origin.
                if (
                    source_text is not None
                    and evidence_quote
                    and source_text[evidence_start:evidence_end] != evidence_quote
                ):
                    first = source_text.find(evidence_quote)
                    if first >= 0 and source_text.find(evidence_quote, first + 1) < 0:
                        evidence_start = first
                        evidence_end = first + len(evidence_quote)
                facts.append(
                    ClinicalFact(
                        fact_type=str(item["fact_type"])[:80],
                        value=str(item["value"])[:2_000],
                        evidence_start=evidence_start,
                        evidence_end=evidence_end,
                        evidence_quote=evidence_quote,
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
        if isinstance(raw_warnings, list):
            # Provider text is untrusted and may itself contain PHI. Persist a
            # fixed taxonomy code rather than reflecting model strings.
            warnings = ["PROVIDER_REPORTED_WARNING"] if raw_warnings else []
        else:
            warnings = ["PROVIDER_WARNING_SCHEMA_INVALID"]
        needs_review = bool(raw.get("needs_review", False))
        if invalid_fact:
            warnings.append("PROVIDER_FACT_SCHEMA_INVALID")
            needs_review = True
        return ClinicalNoteDraft(
            summary=str(raw.get("summary", ""))[:20_000],
            facts=facts,
            provider="openai",
            model=model,
            warnings=warnings,
            needs_review=needs_review,
        )

    async def extract(
        self, payload: QualifiedRedactedText, context: ExtractionContext
    ) -> ClinicalNoteDraft:
        if not isinstance(payload, QualifiedRedactedText):
            raise ValueError("REMOTE_TEXT_EGRESS_NOT_QUALIFIED")
        payload.assert_qualified()
        redacted_text = payload.text
        raw = await self._transport(
            self.extract_model, redacted_text, context.interaction_type
        )
        return self._draft_from_raw(
            raw, model=self.extract_model, source_text=redacted_text
        )

    async def review(
        self,
        payload: QualifiedRedactedText,
        context: ExtractionContext,
        primary: ClinicalNoteDraft,
    ) -> ClinicalNoteDraft:
        if not isinstance(payload, QualifiedRedactedText):
            raise ValueError("REMOTE_TEXT_EGRESS_NOT_QUALIFIED")
        payload.assert_qualified()
        redacted_text = payload.text
        if not self.review_model:
            raise ValueError("HIGH_RISK_REVIEW_MODEL_UNAVAILABLE")
        review_payload = json.dumps(
            {
                "redacted_source": redacted_text,
                "primary": {
                    "summary": primary.summary,
                    "facts": [
                        {
                            "fact_type": fact.fact_type,
                            "value": fact.value,
                            "evidence_start": fact.evidence_start,
                            "evidence_end": fact.evidence_end,
                            "evidence_quote": fact.evidence_quote,
                            "feature_keys": fact.feature_keys,
                            "critical": fact.critical,
                        }
                        for fact in primary.facts
                    ],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        raw = await self._transport(
            self.review_model,
            review_payload,
            f"{context.interaction_type}:review",
        )
        return self._draft_from_raw(
            raw, model=self.review_model, source_text=redacted_text
        )
