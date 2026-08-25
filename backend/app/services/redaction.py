from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from app.core.field_crypto import field_codec
from app.services.providers.base import (
    ClinicalNoteDraft,
    ClinicalNoteProvider,
    ExtractionContext,
    validate_evidence,
)

logger = logging.getLogger("nightingale.redaction")


@dataclass(frozen=True)
class DetectedEntity:
    entity_type: str
    start: int
    end: int
    score: float = 1.0


class AnalyzerAdapter(Protocol):
    def analyze(self, text: str) -> list[DetectedEntity]: ...


class PresidioAnalyzerAdapter:
    """Lazy Presidio adapter so the demo never downloads an NLP model."""

    def __init__(self, model_name: str) -> None:
        if importlib.util.find_spec(model_name) is None:
            raise RuntimeError("PRESIDIO_NLP_MODEL_UNAVAILABLE")
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": model_name}],
            }
        )
        self.engine = AnalyzerEngine(
            nlp_engine=provider.create_engine(), supported_languages=["en"]
        )

    def analyze(self, text: str) -> list[DetectedEntity]:
        results = self.engine.analyze(
            text=text,
            language="en",
            entities=["PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "ID"],
        )
        return [
            DetectedEntity(item.entity_type, item.start, item.end, item.score)
            for item in results
        ]


@dataclass(frozen=True)
class RedactionResult:
    normalized_text: str
    redacted_text: str
    entity_counts: dict[str, int]
    map_ciphertext: bytes
    input_sha256: str
    redacted_sha256: str
    status: str
    needs_review: bool
    residual_scan_passed: bool
    error_code: str | None
    remote_egress_allowed: bool


@dataclass(frozen=True)
class PipelineResult:
    redaction: RedactionResult
    draft: ClinicalNoteDraft
    used_fallback: bool


NRIC_FIN = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE)
MRN = re.compile(r"\bMRN[\s:#-]*[A-Z0-9-]{5,20}\b", re.IGNORECASE)
SG_PHONE = re.compile(r"(?<!\d)(?:\+65[\s-]?)?[3689]\d{3}[\s-]?\d{4}(?!\d)")
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class RedactionService:
    def __init__(
        self,
        *,
        analyzer: AnalyzerAdapter | None = None,
        require_presidio: bool = True,
        presidio_model: str = "en_core_web_sm",
    ) -> None:
        self._analyzer = analyzer
        self.require_presidio = require_presidio
        self.presidio_model = presidio_model

    def _load_analyzer(self) -> AnalyzerAdapter:
        if self._analyzer is None:
            self._analyzer = PresidioAnalyzerAdapter(self.presidio_model)
        return self._analyzer

    @staticmethod
    def _deterministic_entities(
        text: str, known_names: list[str]
    ) -> list[DetectedEntity]:
        output: list[DetectedEntity] = []
        for name in known_names:
            normalized = unicodedata.normalize("NFC", name).strip()
            if normalized:
                output.extend(
                    DetectedEntity("KNOWN_NAME", match.start(), match.end())
                    for match in re.finditer(re.escape(normalized), text, re.IGNORECASE)
                )
        for entity_type, pattern in (
            ("NRIC_FIN", NRIC_FIN),
            ("MRN", MRN),
            ("SG_PHONE", SG_PHONE),
            ("EMAIL", EMAIL),
        ):
            output.extend(
                DetectedEntity(entity_type, match.start(), match.end())
                for match in pattern.finditer(text)
            )
        return output

    @staticmethod
    def _non_overlapping(
        entities: list[DetectedEntity], text_length: int
    ) -> list[DetectedEntity]:
        valid = [item for item in entities if 0 <= item.start < item.end <= text_length]
        valid.sort(key=lambda item: (item.start, -(item.end - item.start)))
        selected: list[DetectedEntity] = []
        for item in valid:
            if not selected or item.start >= selected[-1].end:
                selected.append(item)
                continue
            previous = selected[-1]
            # Crossing aliases such as "Mary Ann" and "Ann Lee" must expand
            # to their interval union. Dropping the second overlapping span
            # would leave " Lee" outside the placeholder and falsely pass the
            # deterministic residual scan.
            if item.end > previous.end:
                selected[-1] = DetectedEntity(
                    previous.entity_type,
                    previous.start,
                    item.end,
                    max(previous.score, item.score),
                )
        return selected

    @classmethod
    def _replace(
        cls, text: str, entities: list[DetectedEntity]
    ) -> tuple[str, list[dict[str, object]], Counter[str]]:
        counts: Counter[str] = Counter()
        chunks: list[str] = []
        redaction_map: list[dict[str, object]] = []
        cursor = 0
        for item in cls._non_overlapping(entities, len(text)):
            chunks.append(text[cursor : item.start])
            counts[item.entity_type] += 1
            placeholder = f"[{item.entity_type}_{counts[item.entity_type]}]"
            chunks.append(placeholder)
            redaction_map.append(
                {
                    "entity_type": item.entity_type,
                    "original": text[item.start : item.end],
                    "start": item.start,
                    "end": item.end,
                    "placeholder": placeholder,
                }
            )
            cursor = item.end
        chunks.append(text[cursor:])
        return "".join(chunks), redaction_map, counts

    def redact(
        self,
        text: str,
        *,
        clinic_id: uuid.UUID,
        record_id: uuid.UUID,
        known_names: list[str] | None = None,
    ) -> RedactionResult:
        normalized = unicodedata.normalize("NFC", text)
        names = list(known_names or [])
        redacted, redaction_map, counts = self._replace(
            normalized, self._deterministic_entities(normalized, names)
        )
        status = "completed"
        needs_review = False
        error_code: str | None = None
        analyzer: AnalyzerAdapter | None = None
        if self.require_presidio or self._analyzer is not None:
            try:
                analyzer = self._load_analyzer()
                redacted, presidio_map, presidio_counts = self._replace(
                    redacted, analyzer.analyze(redacted)
                )
                redaction_map.extend(presidio_map)
                counts.update(presidio_counts)
            except Exception:
                status = "fallback"
                needs_review = True
                error_code = "PRESIDIO_UNAVAILABLE"

        residuals = self._deterministic_entities(redacted, names)
        if analyzer is not None and error_code is None:
            try:
                residuals.extend(analyzer.analyze(redacted))
            except Exception:
                status = "fallback"
                needs_review = True
                error_code = "PRESIDIO_UNAVAILABLE"
        if residuals:
            status = "fallback"
            needs_review = True
            error_code = "RESIDUAL_PHI_DETECTED"
        residual_passed = not residuals and error_code is None
        remote_allowed = residual_passed and (
            analyzer is not None or not self.require_presidio
        )
        encrypted_map = field_codec.encrypt_json(
            clinic_id, "redaction.map", record_id, redaction_map
        )
        logger.info(
            "redaction_finished status=%s error_code=%s entity_counts=%s",
            status,
            error_code or "NONE",
            dict(sorted(counts.items())),
        )
        return RedactionResult(
            normalized_text=normalized,
            redacted_text=redacted,
            entity_counts=dict(counts),
            map_ciphertext=encrypted_map,
            input_sha256=hashlib.sha256(normalized.encode()).hexdigest(),
            redacted_sha256=hashlib.sha256(redacted.encode()).hexdigest(),
            status=status,
            needs_review=needs_review,
            residual_scan_passed=residual_passed,
            error_code=error_code,
            remote_egress_allowed=remote_allowed,
        )


class ClinicalScribePipeline:
    def __init__(
        self,
        redaction_service: RedactionService,
        *,
        fallback_provider: ClinicalNoteProvider,
    ) -> None:
        self.redaction_service = redaction_service
        self.fallback_provider = fallback_provider

    async def run(
        self,
        source_text: str,
        *,
        context: ExtractionContext,
        known_names: list[str] | None = None,
        remote_provider: ClinicalNoteProvider | None = None,
    ) -> PipelineResult:
        redaction = self.redaction_service.redact(
            source_text,
            clinic_id=context.clinic_id,
            record_id=context.source_version_id,
            known_names=known_names,
        )
        use_remote = remote_provider is not None and redaction.remote_egress_allowed
        provider = remote_provider if use_remote else self.fallback_provider
        assert provider is not None
        provider_text = (
            redaction.redacted_text if use_remote else redaction.normalized_text
        )
        draft = validate_evidence(
            await provider.extract(provider_text, context), provider_text
        )
        if redaction.needs_review and not draft.needs_review:
            draft = ClinicalNoteDraft(
                summary=draft.summary,
                facts=draft.facts,
                provider=draft.provider,
                model=draft.model,
                warnings=[*draft.warnings, redaction.error_code or "REDACTION_REVIEW"],
                needs_review=True,
            )
        return PipelineResult(
            redaction=redaction,
            draft=draft,
            used_fallback=not use_remote,
        )
