from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketState

from app.api.routes import voice_live
from app.core.config import settings
from app.models import (
    CalibrationReport,
    ClinicOperationalSetting,
    EvaluationRun,
    Highlight,
    HighlightSupportReview,
    ProvisionalSafetyAlert,
    VoiceSession,
)
from app.services import ai_jobs
from app.services.conflicts import (
    _allergy_assertions_conflict,
    extract_normalized_facts,
    recompute_highlight_conflict_state,
)
from app.services.decisioning import (
    qualify_calibration_report,
    request_parameters_sha256,
)
from app.services.nightingale import (
    _allowlisted_audit_metadata,
    _invalidate_highlight_support_for_source_edit,
)
from app.services.voice import worker as voice_worker
from app.services.voice.live import persist_completed_safety_alerts
from app.services.voice.live_providers import (
    LiveTranscriptEvent,
    LiveTranscriptionError,
)
from app.services.voice.providers.base import TranscriptResult, TranscriptSegmentResult
from app.services.voice.providers.openai_audio import OpenAIAudioTranscriptionProvider
from app.services.voice.worker import _fact_candidates, _normalized_segments

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("language", "text", "scope", "polarity", "key"),
    [
        ("en", "No known drug allergies", "drug_allergies", "absent", "*drug"),
        (
            "ms",
            "Pesakit alahan kepada penisilin",
            "specific_substance",
            "present",
            "penicillin",
        ),
        ("nan", "Bô chai ê kòe-bín", "all_allergies", "absent", "*"),
        ("zh", "对青霉素过敏", "specific_substance", "present", "penicillin"),
    ],
)
def test_trilingual_allergy_normalization_preserves_scope_and_source(
    language: str,
    text: str,
    scope: str,
    polarity: str,
    key: str,
) -> None:
    facts = extract_normalized_facts(text, source_language=language)

    assert len(facts) == 1
    fact = facts[0]
    assert (fact.assertion_scope, fact.polarity, fact.key) == (scope, polarity, key)
    assert fact.source_language == language
    assert fact.quote == text[fact.start : fact.end]


def test_unsupported_language_and_not_documented_never_become_no_allergy() -> None:
    unsupported = extract_normalized_facts("No known allergies", source_language="fr")[
        0
    ]
    undocumented = extract_normalized_facts(
        "Allergies not documented", source_language="en"
    )[0]

    assert unsupported.polarity == "unknown"
    assert unsupported.review_required is True
    assert unsupported.source_language == "und"
    assert undocumented.polarity == "unknown"
    assert undocumented.review_required is True


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "Allergy history is ambiguous"),
        ("ms", "Alahan masih belum pasti"),
        ("nan", "Kòe-bín ê chêng-hêng bē chheng-chhó"),
        ("zh", "过敏情况需要再确认"),
        ("fr", "Allergie à confirmer"),
    ],
)
def test_unqualified_allergy_keyword_falls_back_to_unknown_review(
    language: str, text: str
) -> None:
    facts = extract_normalized_facts(text, source_language=language)

    assert len(facts) == 1
    assert facts[0].fact_type == "allergy"
    assert facts[0].polarity == "unknown"
    assert facts[0].assertion_scope == "all_allergies"
    assert facts[0].review_required is True
    assert facts[0].quote == text[facts[0].start : facts[0].end]


def test_specific_negation_is_not_misclassified_as_global_nka() -> None:
    facts = extract_normalized_facts(
        "No known drug allergy to penicillin", source_language="en"
    )

    assert [(item.assertion_scope, item.key, item.polarity) for item in facts] == [
        ("specific_substance", "penicillin", "absent")
    ]


@pytest.mark.parametrize(
    ("text", "key", "category", "review_required"),
    [
        ("Patient is allergic to penicillin", "penicillin", "drug", False),
        ("Patient is allergic to peanut", "peanut", "food", False),
        ("Patient is allergic to latex", "latex", "environmental", False),
        ("Patient is allergic to banana", "banana", None, True),
        ("No known drug allergies", "*drug", "drug", False),
        ("No known allergies", "*", None, False),
    ],
)
def test_allergy_category_uses_only_the_audited_concept_map(
    text: str,
    key: str,
    category: str | None,
    review_required: bool,
) -> None:
    fact = extract_normalized_facts(text, source_language="en")[0]

    assert fact.key == key
    assert fact.allergy_category == category
    assert fact.review_required is review_required


def test_broad_nka_conflict_is_symmetric_and_unknown_is_not_reassuring() -> None:
    broad = SimpleNamespace(
        polarity="absent",
        assertion_scope="all_allergies",
        allergy_category=None,
    )
    named = SimpleNamespace(
        polarity="present",
        assertion_scope="specific_substance",
        allergy_category="drug",
    )
    unknown = SimpleNamespace(
        polarity="unknown",
        assertion_scope="all_allergies",
        allergy_category=None,
    )

    assert _allergy_assertions_conflict(broad, "*", named, "penicillin") is True  # type: ignore[arg-type]
    assert _allergy_assertions_conflict(named, "penicillin", broad, "*") is True  # type: ignore[arg-type]
    assert _allergy_assertions_conflict(unknown, "*", named, "penicillin") is False  # type: ignore[arg-type]


def test_nkda_scope_overlaps_only_audited_drug_concepts() -> None:
    nkda = SimpleNamespace(
        polarity="absent",
        assertion_scope="drug_allergies",
        allergy_category="drug",
    )
    drug = SimpleNamespace(
        polarity="present",
        assertion_scope="specific_substance",
        allergy_category="drug",
    )
    food = SimpleNamespace(
        polarity="present",
        assertion_scope="specific_substance",
        allergy_category="food",
    )
    environmental = SimpleNamespace(
        polarity="present",
        assertion_scope="specific_substance",
        allergy_category="environmental",
    )
    unavailable = SimpleNamespace(
        polarity="present",
        assertion_scope="specific_substance",
        allergy_category=None,
    )

    assert _allergy_assertions_conflict(nkda, "*drug", drug, "penicillin") is True  # type: ignore[arg-type]
    assert _allergy_assertions_conflict(drug, "penicillin", nkda, "*drug") is True  # type: ignore[arg-type]
    assert _allergy_assertions_conflict(nkda, "*drug", food, "peanut") is False  # type: ignore[arg-type]
    assert _allergy_assertions_conflict(nkda, "*drug", environmental, "latex") is False  # type: ignore[arg-type]
    assert _allergy_assertions_conflict(nkda, "*drug", unavailable, "banana") is False  # type: ignore[arg-type]


class _FirstResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value


class _RowsResult:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def first(self) -> Any:
        return self.values[0] if self.values else None

    def all(self) -> list[Any]:
        return self.values


class _SequenceSession:
    def __init__(self, results: list[_RowsResult]) -> None:
        self.results = iter(results)
        self.added: list[object] = []

    def exec(self, _statement: Any) -> _RowsResult:
        return next(self.results)

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None


@pytest.mark.parametrize("remaining_conflict", [False, True])
def test_conflict_state_recompute_changes_only_mutable_unresolved_state(
    remaining_conflict: bool,
) -> None:
    clinic_id = uuid.uuid4()
    patient_id = uuid.uuid4()
    highlight = Highlight(
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry_id=uuid.uuid4(),
        source_entry_version_id=uuid.uuid4(),
        label_ciphertext=b"fixture",
        status="accepted",
        critical=False,
        unresolved=True,
        feature_keys_json=[],
        created_by_id=uuid.uuid4(),
    )
    conflicts = [SimpleNamespace(severity="critical")] if remaining_conflict else []
    session = _SequenceSession(
        [
            _RowsResult([highlight]),
            _RowsResult([uuid.uuid4()]),
            _RowsResult(conflicts),
        ]
    )
    context = SimpleNamespace(clinic_id=clinic_id)

    affected = recompute_highlight_conflict_state(
        session,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        {highlight.id},
    )

    assert affected == {patient_id}
    assert highlight.unresolved is remaining_conflict
    assert highlight.critical is False
    assert "risk:critical" not in highlight.feature_keys_json


def test_patient_owned_source_edit_invalidates_support_without_learning() -> None:
    clinic_id = uuid.uuid4()
    patient_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    source_version_id = uuid.uuid4()
    observed_version_id = uuid.uuid4()
    highlight = Highlight(
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry_id=entry_id,
        source_entry_version_id=source_version_id,
        label_ciphertext=b"fixture",
        status="accepted",
        created_by_id=uuid.uuid4(),
    )
    session = _SequenceSession(
        [_RowsResult([highlight]), _RowsResult([]), _RowsResult([])]
    )
    context = SimpleNamespace(clinic_id=clinic_id, role="patient")
    entry = SimpleNamespace(id=entry_id)
    next_version = SimpleNamespace(id=observed_version_id)

    related, affected = _invalidate_highlight_support_for_source_edit(
        session,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        entry,  # type: ignore[arg-type]
        next_version,  # type: ignore[arg-type]
    )

    assert related == [highlight]
    assert affected == {patient_id}
    assert highlight.support_state == "historical"
    assert highlight.support_review_required is True
    assert highlight.current_priority_eligible is False
    reviews = [
        item for item in session.added if isinstance(item, HighlightSupportReview)
    ]
    assert len(reviews) == 1
    assert reviews[0].source_entry_version_id == source_version_id
    assert reviews[0].observed_current_version_id == observed_version_id


class _CalibrationSession:
    def __init__(self, run: EvaluationRun | None) -> None:
        self.run = run

    def exec(self, _statement: Any) -> _FirstResult:
        return _FirstResult(self.run)


def _calibration_pair(
    *, expires_at: datetime
) -> tuple[EvaluationRun, CalibrationReport]:
    clinic_id = uuid.uuid4()
    parameters: dict[str, object] = {"schema": "clinical-fact-v2"}
    run = EvaluationRun(
        clinic_id=clinic_id,
        provider="fixture",
        exact_model_id="fixture-model",
        task="clinical_fact_extraction",
        request_parameters_json=parameters,
        dataset_manifest_sha256="a" * 64,
        code_commit="b" * 40,
        calibration_split="calibration",
        holdout_split="holdout",
        total_sample_count=160,
        calibration_sample_count=40,
        holdout_sample_count=120,
        sample_count=120,
        status="completed",
    )
    report = CalibrationReport(
        clinic_id=clinic_id,
        evaluation_run_id=run.id,
        provider=run.provider,
        exact_model_id=run.exact_model_id,
        task=run.task,
        request_parameters_sha256=request_parameters_sha256(parameters),
        dataset_manifest_sha256=run.dataset_manifest_sha256,
        code_commit=run.code_commit,
        total_sample_count=run.total_sample_count,
        calibration_sample_count=run.calibration_sample_count,
        holdout_sample_count=run.holdout_sample_count,
        sample_count=run.sample_count,
        consultation_count=20,
        confidence_band="high",
        accuracy_lower_bound=0.91,
        expires_at=expires_at,
    )
    return run, report


def test_calibration_is_requalified_for_expiry_and_identity_consistency() -> None:
    run, report = _calibration_pair(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    expired = qualify_calibration_report(_CalibrationSession(run), report)  # type: ignore[arg-type]
    report.expires_at = datetime.now(UTC) + timedelta(days=1)
    # Keep the report internally coherent while changing only its holdout
    # population identity; qualification must compare explicit holdout counts.
    report.total_sample_count -= 1
    report.holdout_sample_count -= 1
    report.sample_count -= 1
    inconsistent = qualify_calibration_report(_CalibrationSession(run), report)  # type: ignore[arg-type]

    assert expired.qualified is False
    assert "CALIBRATION_REPORT_EXPIRED" in expired.reasons
    assert inconsistent.qualified is False
    assert "CALIBRATION_SAMPLE_COUNT_INCONSISTENT" in inconsistent.reasons


def test_calibration_rejects_negative_explicit_sample_accounting() -> None:
    run, report = _calibration_pair(expires_at=datetime.now(UTC) + timedelta(days=1))
    report.calibration_sample_count = -1
    report.total_sample_count = report.holdout_sample_count - 1

    result = qualify_calibration_report(_CalibrationSession(run), report)  # type: ignore[arg-type]

    assert result.qualified is False
    assert "CALIBRATION_SAMPLE_COUNTS_INCONSISTENT" in result.reasons


def test_calibration_rejects_placeholder_identities_and_non_finite_metrics() -> None:
    run, report = _calibration_pair(expires_at=datetime.now(UTC) + timedelta(days=1))
    run.dataset_manifest_sha256 = "0" * 64
    report.dataset_manifest_sha256 = "0" * 64
    run.code_commit = "unknown"
    report.code_commit = "unknown"
    report.metrics_json = {"accuracy": float("nan")}

    result = qualify_calibration_report(_CalibrationSession(run), report)  # type: ignore[arg-type]

    assert result.qualified is False
    assert "CALIBRATION_DATASET_IDENTITY_INVALID" in result.reasons
    assert "CALIBRATION_CODE_IDENTITY_INVALID" in result.reasons
    assert "CALIBRATION_METRICS_NON_FINITE" in result.reasons


def test_calibration_rejects_malformed_non_hex_code_revision() -> None:
    run, report = _calibration_pair(expires_at=datetime.now(UTC) + timedelta(days=1))
    run.code_commit = "commit-fixture"
    report.code_commit = "commit-fixture"

    result = qualify_calibration_report(_CalibrationSession(run), report)  # type: ignore[arg-type]

    assert result.qualified is False
    assert "CALIBRATION_CODE_IDENTITY_INVALID" in result.reasons


def test_audit_metadata_drops_free_text_and_keeps_machine_codes() -> None:
    safe = _allowlisted_audit_metadata(
        {
            "reason": "patient says private clinical text",
            "reason_code": "clinical_review_requested",
            "version_id": str(uuid.uuid4()),
        }
    )

    assert "reason" not in safe
    assert safe["reason_code"] == "clinical_review_requested"
    assert "version_id" in safe


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.operational = ClinicOperationalSetting(
            clinic_id=uuid.uuid4(),
            supported_languages_json=["en", "ms", "nan", "zh", "cmn"],
        )

    def exec(self, statement: Any) -> _FirstResult:
        if "clinic_operational_settings" in str(statement):
            return _FirstResult(self.operational)
        return _FirstResult(None)

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None


def test_completed_live_segment_persists_addressable_provisional_alert() -> None:
    db = _FakeDB()
    clinic_id = uuid.uuid4()
    voice_session = VoiceSession(
        clinic_id=clinic_id,
        patient_id=uuid.uuid4(),
        capture_kind="clinical",
        created_by_id=uuid.uuid4(),
    )
    context = SimpleNamespace(
        clinic_id=clinic_id,
        user_id=uuid.uuid4(),
        membership=SimpleNamespace(id=uuid.uuid4()),
    )
    completed_at = datetime.now(UTC)

    alerts = persist_completed_safety_alerts(
        db,  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        voice_session,
        source_event_id="segment-2",
        text="  Pesakit alahan kepada penisilin.",
        source_language="ms",
        completed_segment_at=completed_at,
    )

    assert len(alerts) == 1
    alert = alerts[0]
    assert isinstance(alert, ProvisionalSafetyAlert)
    assert alert.state == "pending"
    assert alert.concept_code == "allergy:penicillin"
    assert alert.source_language == "ms"
    assert alert.source_start_offset == 10
    assert alert.detected_at - completed_at < timedelta(seconds=5)
    assert alert.confirmed_assertion_id is None


class _FakeWebSocket:
    def __init__(self, *, commit_delay: float = 0.02) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.commit_delay = commit_delay
        self.calls = 0
        self.sent: list[dict[str, object]] = []

    async def receive(self) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(self.commit_delay)
            return {"type": "websocket.receive", "text": '{"type":"commit"}'}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def close(self, *, code: int) -> None:
        del code
        self.application_state = WebSocketState.DISCONNECTED


class _HangingConnection:
    provider_name = "fixture"
    model = "fixture-model"
    remote_audio_egress_required = False

    def __init__(self, *, emit_delta: bool) -> None:
        self.calls = 0
        self.emit_delta = emit_delta

    async def send_audio(self, _pcm16: bytes) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def receive_event(self) -> LiveTranscriptEvent:
        self.calls += 1
        if self.calls == 1:
            return LiveTranscriptEvent(kind="ready")
        if self.calls == 2 and self.emit_delta:
            await asyncio.sleep(0.04)
            return LiveTranscriptEvent(kind="delta", text="penicillin")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


class _CompletedTurnConnection:
    provider_name = "fixture"
    model = "fixture-model"
    remote_audio_egress_required = False

    def __init__(self) -> None:
        self.calls = 0

    async def send_audio(self, _pcm16: bytes) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def receive_event(self) -> LiveTranscriptEvent:
        self.calls += 1
        if self.calls == 1:
            return LiveTranscriptEvent(kind="ready")
        if self.calls == 2:
            await asyncio.sleep(0.02)
            return LiveTranscriptEvent(
                kind="completed",
                text="Patient is allergic to penicillin.",
                item_id="turn-120",
                source_language="en",
            )
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


class _CommitThenDisconnectWebSocket:
    def __init__(self) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.calls = 0
        self.sent: list[dict[str, object]] = []

    async def receive(self) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.01)
            return {"type": "websocket.receive", "text": '{"type":"commit"}'}
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def close(self, *, code: int) -> None:
        del code
        self.application_state = WebSocketState.DISCONNECTED


def test_live_route_captures_completion_before_alert_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _CommitThenDisconnectWebSocket()
    connection = _CompletedTurnConnection()
    context = SimpleNamespace(clinic_id=uuid.uuid4())
    completed_at = datetime(2026, 9, 2, 9, 2, tzinfo=UTC)
    observed: dict[str, object] = {}

    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_FRAME_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(voice_live, "get_datetime_utc", lambda: completed_at)
    monkeypatch.setattr(
        voice_live, "_authorized_context", lambda *_args, **_kwargs: context
    )
    monkeypatch.setattr(
        voice_live,
        "_persist_status",
        lambda *_args, **_kwargs: "available",
    )

    def persist(*_args: object, **kwargs: object) -> list[uuid.UUID]:
        observed.update(kwargs)
        return [uuid.UUID("00000000-0000-0000-0000-000000000125")]

    monkeypatch.setattr(voice_live, "_persist_completed_alerts", persist)

    asyncio.run(
        voice_live._run_live_session(  # noqa: SLF001
            websocket,  # type: ignore[arg-type]
            connection,
            "fixture-token",
            context,  # type: ignore[arg-type]
            uuid.uuid4(),
        )
    )

    assert observed["completed_segment_at"] == completed_at
    completed_payload = next(
        item for item in websocket.sent if item["type"] == "transcript.completed"
    )
    assert completed_payload["provisional_alert_ids"] == [
        "00000000-0000-0000-0000-000000000125"
    ]


class _TwoFrameWebSocket:
    def __init__(self) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.calls = 0
        self.sent: list[dict[str, object]] = []

    async def receive(self) -> dict[str, object]:
        self.calls += 1
        if self.calls <= 2:
            # Let the provider's ready event win the initial race, then model
            # two distinct outbound audio frames on one upstream connection.
            await asyncio.sleep(0.02)
            return {"type": "websocket.receive", "bytes": b"\x00\x00"}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def close(self, *, code: int) -> None:
        del code
        self.application_state = WebSocketState.DISCONNECTED


class _RemoteFrameConnection:
    provider_name = "fixture-remote"
    model = "fixture-model"
    remote_audio_egress_required = True

    def __init__(self) -> None:
        self.ready = True
        self.sent_audio: list[bytes] = []

    async def send_audio(self, pcm16: bytes) -> None:
        self.sent_audio.append(pcm16)

    async def commit(self) -> None:
        return None

    async def receive_event(self) -> LiveTranscriptEvent:
        if self.ready:
            self.ready = False
            return LiveTranscriptEvent(kind="ready")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


def test_live_remote_audio_revocation_fences_the_next_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _TwoFrameWebSocket()
    connection = _RemoteFrameConnection()
    context = SimpleNamespace(clinic_id=uuid.uuid4())
    outbound_checks = 0

    def authorize(
        *_args: object,
        require_remote_audio_egress: bool = False,
        **_kwargs: object,
    ) -> object:
        nonlocal outbound_checks
        if require_remote_audio_egress:
            outbound_checks += 1
            if outbound_checks == 2:
                raise LiveTranscriptionError("REMOTE_AUDIO_EGRESS_CONSENT_REQUIRED")
        return context

    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_FRAME_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(voice_live, "_authorized_context", authorize)
    monkeypatch.setattr(
        voice_live,
        "_persist_status",
        lambda *_a, **_k: "needs_review",
    )

    asyncio.run(
        voice_live._run_live_session(  # noqa: SLF001
            websocket,  # type: ignore[arg-type]
            connection,
            "fixture-token",
            context,  # type: ignore[arg-type]
            uuid.uuid4(),
        )
    )

    assert outbound_checks == 2
    assert connection.sent_audio == [b"\x00\x00"]
    assert any(
        item.get("reason_code") == "REMOTE_AUDIO_EGRESS_CONSENT_REQUIRED"
        for item in websocket.sent
    )


@pytest.mark.parametrize(
    ("emit_delta", "reason"),
    [
        (False, "LIVE_TRANSCRIPT_FIRST_RESULT_TIMEOUT"),
        (True, "LIVE_TRANSCRIPT_OUTPUT_SILENCE"),
    ],
)
def test_live_first_result_and_output_silence_have_independent_deadlines(
    monkeypatch: pytest.MonkeyPatch,
    emit_delta: bool,
    reason: str,
) -> None:
    websocket = _FakeWebSocket()
    connection = _HangingConnection(emit_delta=emit_delta)
    context = SimpleNamespace(clinic_id=uuid.uuid4())
    monkeypatch.setattr(settings, "REMOTE_FIRST_RESULT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_OUTPUT_SILENCE_SECONDS", 0.1)
    monkeypatch.setattr(settings, "LIVE_TRANSCRIPT_FRAME_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(voice_live, "_authorized_context", lambda *_a, **_k: context)
    monkeypatch.setattr(
        voice_live,
        "_persist_status",
        lambda *_a, **_k: "needs_review",
    )

    asyncio.run(
        voice_live._run_live_session(  # noqa: SLF001
            websocket,  # type: ignore[arg-type]
            connection,
            "fixture-token",
            context,  # type: ignore[arg-type]
            uuid.uuid4(),
        )
    )

    assert any(item.get("reason_code") == reason for item in websocket.sent)


def test_importance_learning_defaults_to_shadow() -> None:
    assert settings.IMPORTANCE_LEARNING_MODE == "shadow"


def test_remote_text_provider_requires_explicit_clinic_egress_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    operational = ClinicOperationalSetting(
        clinic_id=clinic_id,
        remote_text_egress_enabled=False,
    )

    class PolicySession:
        def exec(self, _statement: Any) -> _FirstResult:
            return _FirstResult(operational)

    def must_not_load_credentials(*_args: object) -> object:
        raise AssertionError("clinic credentials loaded before egress policy check")

    monkeypatch.setattr(ai_jobs, "clinic_ai_runtime", must_not_load_credentials)

    assert (
        ai_jobs._configured_remote_provider(  # noqa: SLF001
            PolicySession(),  # type: ignore[arg-type]
            clinic_id,
        )
        is None
    )


def test_batch_remote_audio_provider_uses_remote_deadlines_and_every_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    operational = ClinicOperationalSetting(
        clinic_id=clinic_id,
        remote_audio_egress_enabled=True,
    )

    class PolicySession:
        def exec(self, _statement: Any) -> _FirstResult:
            return _FirstResult(operational)

    voice_session = VoiceSession(
        clinic_id=clinic_id,
        patient_id=uuid.uuid4(),
        capture_kind="clinical",
        created_by_id=uuid.uuid4(),
        remote_audio_consent_at=datetime.now(UTC),
    )
    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "openai")
    monkeypatch.setattr(settings, "STRICT_NO_AUDIO_EGRESS", False)
    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", True)
    monkeypatch.setattr(settings, "REMOTE_REQUEST_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(settings, "REMOTE_CONNECT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(
        voice_worker,
        "clinic_ai_runtime",
        lambda *_args: SimpleNamespace(api_key="TOKEN", transcribe_model="MODEL"),
    )

    provider, reason = voice_worker._configured_provider(  # noqa: SLF001
        PolicySession(),  # type: ignore[arg-type]
        voice_session,
    )

    assert reason is None
    assert isinstance(provider, OpenAIAudioTranscriptionProvider)
    assert provider.timeout_seconds == 30.0
    assert provider.connect_timeout_seconds == 5.0


def test_batch_remote_audio_denial_precedes_clinic_credential_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clinic_id = uuid.uuid4()
    operational = ClinicOperationalSetting(
        clinic_id=clinic_id,
        remote_audio_egress_enabled=False,
    )

    class PolicySession:
        def exec(self, _statement: Any) -> _FirstResult:
            return _FirstResult(operational)

    voice_session = VoiceSession(
        clinic_id=clinic_id,
        patient_id=uuid.uuid4(),
        capture_kind="clinical",
        created_by_id=uuid.uuid4(),
    )
    monkeypatch.setattr(settings, "VOICE_TRANSCRIPTION_PROVIDER", "openai")
    monkeypatch.setattr(settings, "STRICT_NO_AUDIO_EGRESS", False)
    monkeypatch.setattr(settings, "REMOTE_AUDIO_EGRESS_ENABLED", True)

    def must_not_load_credentials(*_args: object) -> object:
        raise AssertionError("credentials loaded before audio egress authorization")

    monkeypatch.setattr(voice_worker, "clinic_ai_runtime", must_not_load_credentials)
    provider, reason = voice_worker._configured_provider(  # noqa: SLF001
        PolicySession(),  # type: ignore[arg-type]
        voice_session,
    )
    assert provider is None
    assert reason == "CLINIC_REMOTE_AUDIO_EGRESS_DISABLED"

    operational.remote_audio_egress_enabled = True
    provider, reason = voice_worker._configured_provider(  # noqa: SLF001
        PolicySession(),  # type: ignore[arg-type]
        voice_session,
    )
    assert provider is None
    assert reason == "REMOTE_AUDIO_EGRESS_CONSENT_REQUIRED"


def test_batch_segment_preserves_normalized_source_language_metadata() -> None:
    text = "Pesakit alahan kepada penisilin"
    segment = TranscriptSegmentResult(
        text=text,
        start_ms=0,
        end_ms=2_000,
        speaker_id="patient",
        detected_language="ms-MY",
        confidence=0.9,
        confidence_source="provider",
        overlap_group_id=None,
        text_start=0,
        text_end=len(text),
        source_language="ms-MY",
        language_confidence=0.87,
    )
    result = TranscriptResult(
        text=segment.text,
        segments=[segment],
        provider="fixture",
        model="fixture-model",
        detected_language="ms-MY",
    )

    normalized, warnings = _normalized_segments(  # noqa: SLF001
        result,
        SimpleNamespace(duration_ms=3_000),  # type: ignore[arg-type]
    )

    assert warnings == []
    assert normalized[0].detected_language == "ms"
    assert normalized[0].source_language == "ms"
    assert normalized[0].language_confidence == 0.87


def test_voice_candidate_extraction_keeps_complete_medication_regimen() -> None:
    text = "Started metformin 500mg PO BID"
    segment = TranscriptSegmentResult(
        text=text,
        start_ms=0,
        end_ms=2_000,
        speaker_id="clinician",
        detected_language="en",
        confidence=None,
        confidence_source="unavailable",
        overlap_group_id=None,
        text_start=0,
        text_end=len(text),
        source_language="en",
    )

    candidates = _fact_candidates([segment])  # noqa: SLF001

    assert {candidate[0].fact_type for candidate in candidates} == {
        "medication",
        "dose",
        "route",
        "frequency",
    }
    assert all(text[start:end] == fact.quote for fact, start, end, _ in candidates)
