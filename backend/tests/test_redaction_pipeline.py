import asyncio
import logging
import uuid

import pytest

from app.services.providers.base import ClinicalNoteDraft, ExtractionContext
from app.services.redaction import (
    ClinicalScribePipeline,
    DetectedEntity,
    PresidioAnalyzerAdapter,
    RedactionService,
)

pytestmark = pytest.mark.unit


def test_missing_presidio_model_fails_before_any_download() -> None:
    with pytest.raises(RuntimeError, match="PRESIDIO_NLP_MODEL_UNAVAILABLE"):
        PresidioAnalyzerAdapter("nightingale_model_that_is_not_installed")


class RecordingProvider:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def extract(
        self, redacted_text: str, context: ExtractionContext
    ) -> ClinicalNoteDraft:
        self.inputs.append(redacted_text)
        return ClinicalNoteDraft(
            summary="remote",
            facts=[],
            provider="spy",
            model="spy-model",
        )


class SequenceAnalyzer:
    def __init__(self, responses: list[list[DetectedEntity] | Exception]) -> None:
        self.responses = responses

    def analyze(self, text: str) -> list[DetectedEntity]:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _context() -> ExtractionContext:
    return ExtractionContext(
        clinic_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
    )


def test_remote_provider_only_receives_redacted_synthetic_phi() -> None:
    context = _context()
    remote = RecordingProvider()
    fallback = RecordingProvider()
    service = RedactionService(
        analyzer=SequenceAnalyzer([[], []]), require_presidio=True
    )
    pipeline = ClinicalScribePipeline(service, fallback_provider=fallback)
    source = (
        "Tan Mei Ling NRIC S1234567D MRN A1234567 phone +65 9123 4567 "
        "email mei@example.test reports a penicillin allergy."
    )

    result = asyncio.run(
        pipeline.run(
            source,
            context=context,
            known_names=["Tan Mei Ling"],
            remote_provider=remote,
        )
    )

    assert result.redaction.remote_egress_allowed is True
    assert len(remote.inputs) == 1
    outbound = remote.inputs[0]
    for secret in (
        "Tan Mei Ling",
        "S1234567D",
        "A1234567",
        "+65 9123 4567",
        "mei@example.test",
    ):
        assert secret not in outbound
    assert fallback.inputs == []


def test_presidio_exception_fails_closed_and_logs_no_phi(caplog) -> None:
    context = _context()
    remote = RecordingProvider()
    fallback = RecordingProvider()
    service = RedactionService(
        analyzer=SequenceAnalyzer([RuntimeError("model saw SECRET PATIENT")]),
        require_presidio=True,
    )
    pipeline = ClinicalScribePipeline(service, fallback_provider=fallback)
    source = "Patient Secret Name has NRIC S1234567D and phone 91234567"

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            pipeline.run(
                source,
                context=context,
                known_names=["Patient Secret Name"],
                remote_provider=remote,
            )
        )

    assert result.redaction.status == "fallback"
    assert result.redaction.needs_review is True
    assert result.redaction.error_code == "PRESIDIO_UNAVAILABLE"
    assert result.redaction.remote_egress_allowed is False
    assert remote.inputs == []
    assert len(fallback.inputs) == 1
    joined_logs = " ".join(record.getMessage() for record in caplog.records)
    for secret in ("Patient Secret Name", "S1234567D", "91234567", "SECRET"):
        assert secret not in joined_logs


def test_residual_detection_blocks_remote_provider() -> None:
    context = _context()
    remote = RecordingProvider()
    fallback = RecordingProvider()
    residual = DetectedEntity("PERSON", 0, 8, 0.9)
    service = RedactionService(
        analyzer=SequenceAnalyzer([[], [residual]]), require_presidio=True
    )
    pipeline = ClinicalScribePipeline(service, fallback_provider=fallback)

    result = asyncio.run(
        pipeline.run(
            "A harmless-looking residual sentence.",
            context=context,
            remote_provider=remote,
        )
    )

    assert result.redaction.error_code == "RESIDUAL_PHI_DETECTED"
    assert result.redaction.remote_egress_allowed is False
    assert remote.inputs == []
    assert len(fallback.inputs) == 1
