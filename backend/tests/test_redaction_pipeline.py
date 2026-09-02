import ast
import asyncio
import inspect
import logging
import os
import uuid
from pathlib import Path

import pytest

from app.services.egress import QualifiedRedactedText, TextModelEgressGateway
from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
)
from app.services.providers.openai_text import OpenAITextProvider
from app.services.redaction import (
    ClinicalScribePipeline,
    DetectedEntity,
    PresidioAnalyzerAdapter,
    RedactionService,
)
from app.services.trust_evaluation import _fact_case

pytestmark = pytest.mark.unit


def _direct_text_egress_violations(path: Path, source: str) -> list[str]:
    """Reject every extract/review call except the gateway and audited fallback."""

    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        guarded_methods = {
            "extract",
            "review",
            "transport",
            "_transport",
            "_http_transport",
        }
        if isinstance(node.func, ast.Attribute) and node.func.attr in guarded_methods:
            receiver = ast.unparse(node.func.value)
            gateway_call = (
                isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "TextModelEgressGateway"
            )
            audited_local_fallback = (
                path.name == "redaction.py"
                and node.func.attr == "extract"
                and receiver == "self.fallback_provider"
            )
            audited_provider_transport = (
                path.name == "openai_text.py"
                and node.func.attr == "_transport"
                and receiver == "self"
            )
            if (
                not gateway_call
                and not audited_local_fallback
                and not audited_provider_transport
            ):
                # Record the full dotted call target so an aliased receiver and
                # the specific guarded method are both addressable in the report.
                violations.append(f"{path.name}:{node.lineno}:{ast.unparse(node.func)}")
        elif (
            isinstance(node.func, ast.Call)
            and isinstance(node.func.func, ast.Name)
            and node.func.func.id == "getattr"
            and len(node.func.args) >= 2
            and isinstance(node.func.args[1], ast.Constant)
            and node.func.args[1].value in guarded_methods
        ):
            violations.append(f"{path.name}:{node.lineno}:dynamic-getattr")
    return violations


def test_remote_text_provider_contract_and_call_sites_are_gateway_only() -> None:
    annotation = (
        inspect.signature(OpenAITextProvider.extract).parameters["payload"].annotation
    )
    assert annotation in {"QualifiedRedactedText", QualifiedRedactedText}

    app_root = Path(__file__).parents[1] / "app"
    violations: list[str] = []
    for path in app_root.rglob("*.py"):
        if path == app_root / "services" / "egress.py":
            continue
        violations.extend(_direct_text_egress_violations(path, path.read_text()))
    assert violations == []


def test_gateway_ast_guard_rejects_aliases_and_dynamic_dispatch() -> None:
    mutation = """
async def bypass(provider, client, payload, context):
    await provider.extract(payload, context)
    await client.review(payload, context, None)
    await provider.transport("model", "raw phi", "kind")
    await provider._http_transport("model", "raw phi", "kind")
    await getattr(provider, "extract")(payload, context)
    await getattr(provider, "transport")("model", "raw phi", "kind")
"""
    violations = _direct_text_egress_violations(Path("future_service.py"), mutation)
    assert len(violations) == 6
    assert any(":provider" in item for item in violations)
    assert any(":client" in item for item in violations)
    assert any(":dynamic-getattr" in item for item in violations)
    assert any(":provider.transport" in item for item in violations)
    assert any(":provider._http_transport" in item for item in violations)


def test_missing_presidio_model_fails_before_any_download() -> None:
    with pytest.raises(RuntimeError, match="PRESIDIO_NLP_MODEL_UNAVAILABLE"):
        PresidioAnalyzerAdapter("nightingale_model_that_is_not_installed")


class RecordingProvider:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def extract(
        self, payload: QualifiedRedactedText | str, context: ExtractionContext
    ) -> ClinicalNoteDraft:
        redacted_text = (
            payload.text if isinstance(payload, QualifiedRedactedText) else payload
        )
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


def test_fact_evaluation_redacts_before_typed_gateway_provider_call(
    tmp_path: Path,
) -> None:
    class EvaluationProviderSpy:
        extract_model = "fixture-evaluation-model"

        def __init__(self) -> None:
            self.inputs: list[str] = []

        async def extract(
            self, payload: QualifiedRedactedText, context: ExtractionContext
        ) -> ClinicalNoteDraft:
            del context
            redacted_text = payload.text
            self.inputs.append(redacted_text)
            quote = "penicillin allergy"
            start = redacted_text.index(quote)
            return ClinicalNoteDraft(
                summary="redacted evaluation draft",
                facts=[
                    ClinicalFact(
                        fact_type="allergy",
                        value="penicillin",
                        evidence_start=start,
                        evidence_end=start + len(quote),
                        evidence_quote=quote,
                    )
                ],
                provider="spy",
                model=self.extract_model,
            )

        async def transport(self, *_args: object) -> dict[str, object]:
            raise AssertionError("direct transport bypassed the typed gateway")

    provider = EvaluationProviderSpy()
    outcomes, counts = asyncio.run(
        _fact_case(  # type: ignore[arg-type]
            provider,
            {
                "encounter_id": "fixture-encounter",
                "patient_name": "Alice Tan",
                "dialogue": (
                    "Alice Tan S1234567D reports a penicillin allergy. "
                    "Call +65 9123 4567."
                ),
                "note": "penicillin allergy",
            },
            tmp_path,
            redaction_service=RedactionService(
                analyzer=SequenceAnalyzer([[], []]), require_presidio=True
            ),
        )
    )

    assert outcomes == [True]
    assert counts.get("provider_error", 0) == 0
    assert len(provider.inputs) == 1
    outbound = provider.inputs[0]
    assert "Alice Tan" not in outbound
    assert "S1234567D" not in outbound
    assert "+65 9123 4567" not in outbound
    assert "penicillin allergy" in outbound


def test_typed_egress_gateway_rejects_unqualified_or_tampered_reports() -> None:
    context = _context()
    provider = RecordingProvider()
    service = RedactionService(
        analyzer=SequenceAnalyzer([RuntimeError("synthetic analyzer outage")]),
        require_presidio=True,
    )
    report = service.redact(
        "Patient S1234567D reports pain",
        clinic_id=context.clinic_id,
        record_id=context.source_version_id,
    )

    with pytest.raises(ValueError, match="REMOTE_TEXT_EGRESS_NOT_QUALIFIED"):
        QualifiedRedactedText.from_report(report)
    with pytest.raises(ValueError, match="REMOTE_TEXT_EGRESS_NOT_QUALIFIED"):
        asyncio.run(TextModelEgressGateway(provider).extract(report, context))

    assert provider.inputs == []


def test_openai_provider_revalidates_opaque_payload_before_transport() -> None:
    context = _context()
    report = RedactionService(
        analyzer=SequenceAnalyzer([[], []]), require_presidio=True
    ).redact(
        "Synthetic redacted-safe statement",
        clinic_id=context.clinic_id,
        record_id=context.source_version_id,
    )
    payload = QualifiedRedactedText.from_report(report)
    object.__setattr__(payload, "text", "tampered after qualification")
    transport_called = False

    async def transport(*_args: str) -> dict[str, object]:
        nonlocal transport_called
        transport_called = True
        return {}

    provider = OpenAITextProvider(
        api_key="TOKEN",
        extract_model="fixture-model",
        transport=transport,
    )
    with pytest.raises(ValueError, match="REMOTE_TEXT_EGRESS_NOT_QUALIFIED"):
        asyncio.run(provider.extract(payload, context))
    assert transport_called is False


def test_crossing_known_aliases_are_union_redacted_before_remote_egress() -> None:
    context = _context()
    remote = RecordingProvider()
    fallback = RecordingProvider()
    service = RedactionService(
        analyzer=SequenceAnalyzer([[], []]), require_presidio=True
    )
    pipeline = ClinicalScribePipeline(service, fallback_provider=fallback)

    result = asyncio.run(
        pipeline.run(
            "Mary Ann Lee reports pain",
            context=context,
            known_names=["Mary Ann", "Ann Lee"],
            remote_provider=remote,
        )
    )

    assert result.redaction.remote_egress_allowed is True
    assert result.redaction.residual_scan_passed is True
    assert remote.inputs == ["[KNOWN_NAME_1] reports pain"]
    assert "Mary" not in remote.inputs[0]
    assert "Ann" not in remote.inputs[0]
    assert "Lee" not in remote.inputs[0]
    assert fallback.inputs == []


@pytest.mark.presidio_model
def test_locked_presidio_model_profile_allows_safe_remote_smoke() -> None:
    try:
        __import__("en_core_web_sm")
    except ImportError:
        if os.getenv("REQUIRE_PRESIDIO_MODEL_TEST") == "true":
            pytest.fail("locked Presidio NLP model profile is required in CI")
        pytest.skip("optional locked Presidio NLP model profile is not installed")
    context = _context()
    remote = RecordingProvider()
    fallback = RecordingProvider()
    pipeline = ClinicalScribePipeline(
        RedactionService(require_presidio=True, presidio_model="en_core_web_sm"),
        fallback_provider=fallback,
    )

    result = asyncio.run(
        pipeline.run(
            "Hydration is adequate; email alex@example.test.",
            context=context,
            remote_provider=remote,
        )
    )

    assert result.redaction.remote_egress_allowed is True
    assert result.used_fallback is False
    assert len(remote.inputs) == 1
    assert "alex@example.test" not in remote.inputs[0]
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
