from __future__ import annotations

import asyncio
import json
import random
import struct
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import (
    AudioAsset,
    ClinicFormularyConcept,
    ClinicFormularyConceptCreate,
    ClinicFormularyVersion,
)
from app.services.clinical_formulary import (
    FORMULARY_VERSION,
    FormularyConcept,
    clinic_formulary_content_sha256,
    screen_clinic_medication_regimen,
    screen_medication_regimen,
)
from app.services.conflicts import detect_language_spans, extract_normalized_facts
from app.services.voice.diarization import (
    DiarizationTurn,
    align_diarization_segments,
)
from app.services.voice.ffmpeg import _pcm_signals
from app.services.voice.live_providers import (
    DeterministicLiveTranscriptionConnection,
    OpenAILiveTranscriptionConnection,
)
from app.services.voice.providers.base import TranscriptResult, TranscriptSegmentResult
from app.services.voice.service import audio_quality_public
from app.services.voice.worker import _fact_candidates, _normalized_segments

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "Started metformin 500mg PO BID"),
        ("ms", "Mula metformin 500mg secara oral dua kali sehari"),
        ("nan", "Kha-si metformin 500mg khau-hok chit jit nng pai"),
        ("zh", "开始服用二甲双胍 500毫克 口服 每日两次"),
    ],
)
def test_multilingual_medication_regimen_is_complete_and_source_addressable(
    language: str,
    text: str,
) -> None:
    facts = extract_normalized_facts(text, source_language=language)

    assert {fact.fact_type for fact in facts} == {
        "medication",
        "dose",
        "route",
        "frequency",
    }
    assert {fact.key for fact in facts} == {"metformin"}
    assert {fact.source_language for fact in facts} == {language}
    assert all(text[fact.start : fact.end] == fact.quote for fact in facts)
    assert not any(fact.review_required for fact in facts)


def test_mixed_language_span_falls_back_to_und_but_keeps_qualified_fact_languages() -> (
    None
):
    text = (
        "Patient allergic to penicillin. "
        "Pesakit mula metformin 500mg secara oral dua kali sehari."
    )

    spans = detect_language_spans(text)
    facts = extract_normalized_facts(text)

    assert [span.source_language for span in spans] == ["en", "ms"]
    assert {fact.source_language for fact in facts} == {"en", "ms"}

    code_switched_sentence = "Started metformin dan teruskan dua kali sehari."
    within_clause = detect_language_spans(code_switched_sentence)
    assert [span.source_language for span in within_clause] == ["en", "ms"]
    assert all(span.review_required is False for span in within_clause)
    assert [
        code_switched_sentence[span.start : span.end] for span in within_clause
    ] == ["Started metformin dan ", "teruskan dua kali sehari."]


def test_within_segment_code_switch_preserves_addressable_language_spans() -> None:
    text = (
        "Started metformin 500mg. "
        "Pesakit mula aspirin 100mg secara oral dua kali sehari."
    )
    segment = TranscriptSegmentResult(
        text=text,
        start_ms=0,
        end_ms=4_000,
        speaker_id="SPEAKER_00",
        detected_language="multilingual",
        confidence=0.9,
        confidence_source="provider",
        overlap_group_id=None,
        text_start=0,
        text_end=len(text),
        source_language="multilingual",
        language_confidence=0.84,
    )
    result = TranscriptResult(
        text=text,
        segments=[segment],
        provider="fixture",
        model="within-segment-code-switch-v1",
        detected_language="multilingual",
    )

    normalized, warnings = _normalized_segments(
        result,
        SimpleNamespace(duration_ms=5_000),  # type: ignore[arg-type]
    )

    persisted = normalized[0]
    assert persisted.text == text
    assert persisted.source_language == "und"
    assert persisted.language_confidence is None
    assert "MIXED_LANGUAGE_SEGMENT_REVIEW" in warnings
    assert [span.language_code for span in persisted.language_spans] == ["en", "ms"]
    assert [span.detection_source for span in persisted.language_spans] == [
        "lexicon_rule",
        "lexicon_rule",
    ]
    assert [
        text[span.start_offset : span.end_offset] for span in persisted.language_spans
    ] == [
        "Started metformin 500mg.",
        " Pesakit mula aspirin 100mg secara oral dua kali sehari.",
    ]


def test_no_punctuation_code_switch_keeps_match_level_languages_and_facts() -> None:
    text = (
        "Patient allergic to penicillin pesakit alahan kepada amoksisilin "
        "tùi aspirin kòe-bín"
    )
    result = TranscriptResult(
        text=text,
        segments=[
            TranscriptSegmentResult(
                text=text,
                start_ms=0,
                end_ms=4_000,
                speaker_id="SPEAKER_00",
                detected_language="multilingual",
                source_language="multilingual",
                language_confidence=None,
                confidence=0.9,
                confidence_source="provider",
                overlap_group_id=None,
                text_start=0,
                text_end=len(text),
            )
        ],
        provider="fixture",
        model="within-clause-code-switch-v1",
        detected_language="multilingual",
    )

    normalized, _warnings = _normalized_segments(
        result,
        SimpleNamespace(duration_ms=5_000),  # type: ignore[arg-type]
    )
    segment = normalized[0]
    assert [span.language_code for span in segment.language_spans] == [
        "en",
        "ms",
        "nan",
    ]
    assert all(span.review_required is False for span in segment.language_spans)
    facts = [item[0] for item in _fact_candidates(normalized)]
    assert {(fact.key, fact.source_language, fact.polarity) for fact in facts} == {
        ("penicillin", "en", "present"),
        ("amoxicillin", "ms", "present"),
        ("aspirin", "nan", "present"),
    }


def test_provider_hint_span_retains_qualified_confidence() -> None:
    spans = detect_language_spans(
        "Follow-up consultation",
        source_language="cmn-CN",
        source_confidence=0.87,
    )

    assert len(spans) == 1
    assert spans[0].source_language == "cmn"
    assert spans[0].confidence == 0.87
    assert spans[0].detection_source == "provider_hint"
    assert spans[0].review_required is False


def test_low_confidence_provider_hint_is_addressable_but_requires_review() -> None:
    text = "Follow-up consultation"
    spans = detect_language_spans(
        text,
        source_language="en",
        source_confidence=0.05,
    )

    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == text
    assert spans[0].source_language == "en"
    assert spans[0].confidence == 0.05
    assert spans[0].detection_source == "provider_hint"
    assert spans[0].review_required is True

    clinical_text = "metformin 500mg PO BID"
    result = TranscriptResult(
        text=clinical_text,
        segments=[
            TranscriptSegmentResult(
                text=clinical_text,
                start_ms=0,
                end_ms=1_000,
                speaker_id="SPEAKER_00",
                detected_language="en",
                source_language="en",
                language_confidence=0.05,
                confidence=0.9,
                confidence_source="provider",
                overlap_group_id=None,
                text_start=0,
                text_end=len(clinical_text),
            )
        ],
        provider="fixture",
        model="low-language-confidence-v1",
        detected_language="en",
    )
    normalized, warnings = _normalized_segments(
        result,
        SimpleNamespace(duration_ms=2_000),  # type: ignore[arg-type]
    )
    facts = [item[0] for item in _fact_candidates(normalized)]

    assert "MIXED_LANGUAGE_SEGMENT_REVIEW" in warnings
    assert facts
    assert all(fact.source_language == "en" for fact in facts)
    assert all(fact.review_required is True for fact in facts)


def test_unknown_or_incomplete_medication_candidate_is_never_silent() -> None:
    facts = extract_normalized_facts("Started mysterydrug", source_language="en")

    assert len(facts) == 1
    assert facts[0].fact_type == "medication"
    assert facts[0].key == "mysterydrug"
    assert facts[0].review_required is True


def test_versioned_formulary_fails_closed_for_unknown_range_and_allergy() -> None:
    qualified = screen_medication_regimen(
        medication="二甲双胍",
        dose_value=500,
        dose_unit="毫克",
        route="口服",
        frequency="每日两次",
    )
    unknown = screen_medication_regimen(
        medication="mysterydrug",
        dose_value=5,
        dose_unit="mg",
        route="oral",
        frequency="daily",
    )
    out_of_range = screen_medication_regimen(
        medication="metformin",
        dose_value=5_000,
        dose_unit="mg",
        route="oral",
        frequency="daily",
    )
    contraindicated = screen_medication_regimen(
        medication="amoksisilin",
        dose_value=500,
        dose_unit="mg",
        route="oral",
        frequency="tiga kali sehari",
        active_allergy_concepts=["penicillin"],
    )

    assert qualified.eligible is True
    assert qualified.formulary_version == FORMULARY_VERSION
    assert qualified.canonical_name == "metformin"
    assert unknown.reason_codes == ("MEDICATION_CONCEPT_UNKNOWN",)
    assert "DOSE_OUT_OF_SCREENING_RANGE" in out_of_range.reason_codes
    assert contraindicated.reason_codes == ("ACTIVE_ALLERGY_CONTRAINDICATION",)


def test_audited_clinic_formulary_version_qualifies_by_content_digest() -> None:
    clinic_id = uuid.uuid4()
    version_id = uuid.uuid4()
    now = datetime.now(UTC)
    concept = FormularyConcept(
        code="rxnorm:860975",
        display_name="metformin",
        aliases=("metformin", "二甲双胍"),
        dose_unit="mg",
        minimum_single_dose=250,
        maximum_single_dose=1_000,
        permitted_routes=frozenset({"oral"}),
    )
    configuration = ClinicFormularyConceptCreate(
        concept_code=concept.code,
        canonical_name=concept.display_name,
        multilingual_aliases={
            "en": ["metformin"],
            "ms": ["metformin"],
            "nan": ["metformin"],
            "zh": ["二甲双胍"],
        },
        dose_unit="mg",
        minimum_single_dose=250,
        maximum_single_dose=1_000,
        permitted_routes=["oral"],
        contraindicated_allergy_concepts=[],
    )
    version = ClinicFormularyVersion(
        id=version_id,
        clinic_id=clinic_id,
        version_code="clinic-a-formulary-2026-09",
        status="active",
        content_sha256=clinic_formulary_content_sha256([configuration]),
        effective_at=now,
        content_locked_at=now,
        qualified_at=now,
        qualification_source="clinic_admin",
    )
    row = ClinicFormularyConcept(
        clinic_id=clinic_id,
        formulary_version_id=version_id,
        concept_code=concept.code,
        canonical_name=concept.display_name,
        multilingual_aliases_json=configuration.multilingual_aliases,
        dose_unit="mg",
        minimum_single_dose=250,
        maximum_single_dose=1_000,
        permitted_routes_json=["oral"],
        contraindicated_allergy_concepts_json=[],
    )

    class Rows:
        def __init__(self, values: list[Any]) -> None:
            self.values = values

        def all(self) -> list[Any]:
            return self.values

    class FormularySession:
        def __init__(self) -> None:
            self.results = iter(([version], [row]))

        def exec(self, _statement: Any) -> Rows:
            return Rows(list(next(self.results)))

    result = screen_clinic_medication_regimen(
        FormularySession(),  # type: ignore[arg-type]
        clinic_id=clinic_id,
        medication="二甲双胍",
        dose_value=500,
        dose_unit="mg",
        route="oral",
        frequency="twice daily",
    )

    assert result.eligible is True
    assert result.qualification_source == "clinic_version"
    assert result.formulary_version == "clinic-a-formulary-2026-09"


class _Transport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return json.dumps({"type": "session.updated"})

    async def close(self) -> None:
        return None


async def _exercise_live_provider_server_vad() -> None:
    transport = _Transport()
    remote = OpenAILiveTranscriptionConnection(
        transport=transport,
        model="gpt-live-transcribe-fixture",
        max_frame_bytes=8_192,
    )
    await remote.configure()
    configured = json.loads(transport.sent[0])
    turn_detection = configured["session"]["audio"]["input"]["turn_detection"]
    assert turn_detection == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 500,
    }

    live = DeterministicLiveTranscriptionConnection(
        model="code-switch-overlap-v1",
        max_frame_bytes=8_192,
    )
    assert (await live.receive_event()).kind == "ready"
    await live.send_audio(b"\x00\x00")
    delta = await live.receive_event()
    completed = await live.receive_event()
    assert delta.kind == "delta"
    assert completed.kind == "completed"
    assert completed.source_language == "en"
    # A second turn remains possible; the completed event was server-VAD-like,
    # not a final recording commit.
    await live.send_audio(b"\x00\x00")
    assert (await live.receive_event()).kind == "delta"


def test_live_provider_uses_server_vad_and_completes_before_final_commit() -> None:
    asyncio.run(_exercise_live_provider_server_vad())


def _write_wav(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_audio_quality_distinguishes_low_signal_from_low_snr_noise(
    tmp_path: Path,
) -> None:
    quiet_path = tmp_path / "quiet.wav"
    noisy_path = tmp_path / "noise.wav"
    _write_wav(quiet_path, [100 if index % 2 else -100 for index in range(8_000)])
    generator = random.Random(7)
    _write_wav(noisy_path, [generator.randint(-2_000, 2_000) for _ in range(8_000)])

    _duration, quiet = _pcm_signals(quiet_path, multi_device=False)
    _duration, noisy = _pcm_signals(noisy_path, multi_device=False)

    assert quiet["schema_version"] == "nightingale-audio-quality-v1"
    assert quiet["low_signal_review"] is True
    assert quiet["noise_review"] is False
    assert noisy["low_signal_review"] is False
    assert noisy["noise_review"] is True
    assert float(noisy["estimated_snr_db"]) < 12
    assert "noise_floor_dbfs" in noisy
    assert "zero_crossing_rate" in noisy


@pytest.mark.parametrize(
    ("fixture_name", "samples", "expected_review"),
    [
        (
            "quiet",
            [100 if index % 2 else -100 for index in range(8_000)],
            "low_signal_review",
        ),
        (
            "clipped",
            [32_767 if index % 2 else -32_767 for index in range(8_000)],
            "clipping_review",
        ),
        (
            "noisy",
            [random.Random(index).randint(-2_000, 2_000) for index in range(8_000)],
            "noise_review",
        ),
    ],
)
def test_typed_audio_quality_projection_allowlists_review_evidence(
    tmp_path: Path,
    fixture_name: str,
    samples: list[int],
    expected_review: str,
) -> None:
    source = tmp_path / f"{fixture_name}.wav"
    _write_wav(source, samples)
    _duration, source_signals = _pcm_signals(source, multi_device=False)
    persisted = {
        **source_signals,
        "measurement_stage": "decoded-pre-normalization",
        "working_copy": True,
        "denoise_applied": True,
        "denoise_filter": "must-not-cross-api-boundary",
        "processing_chain_version": "nightingale-voice-working-copy-v1",
        "device_signals": [{"private-diagnostic": "must-not-cross"}],
        "normalized_output_signals": {"private-diagnostic": "must-not-cross"},
    }
    asset = AudioAsset(
        clinic_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        payload_ciphertext=b"encrypted-fixture",
        plaintext_sha256="0" * 64,
        duration_ms=500,
        preprocessing_json=persisted,
    )

    quality, unavailable_reason = audio_quality_public(asset)

    assert unavailable_reason is None
    assert quality is not None
    body = quality.model_dump()
    assert body[expected_review] is True
    assert body["denoise_applied"] is True
    assert body["review_required"] is True
    assert "denoise_filter" not in body
    assert "device_signals" not in body
    assert "normalized_output_signals" not in body


def test_typed_audio_quality_projection_fails_closed_for_malformed_metadata() -> None:
    asset = AudioAsset(
        clinic_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        payload_ciphertext=b"encrypted-fixture",
        plaintext_sha256="0" * 64,
        duration_ms=500,
        preprocessing_json={
            "measurement_stage": "decoded-pre-normalization",
            "processing_chain_version": "nightingale-voice-working-copy-v1",
            "rms": "100.0",
            "noise_floor_dbfs": -20.0,
            "estimated_snr_db": 10.0,
            "clipping_ratio": 0.0,
            "silence_ratio": 0.0,
            "silence_review": False,
            "clipping_review": False,
            "low_signal_review": False,
            "noise_review": False,
            "overlap_review": False,
            "denoise_applied": True,
        },
    )

    quality, unavailable_reason = audio_quality_public(asset)

    assert quality is None
    assert unavailable_reason == "AUDIO_QUALITY_METADATA_INVALID"
    assert audio_quality_public(None) == (None, "AUDIO_ASSET_NOT_AVAILABLE")


def test_pyannote_alignment_preserves_all_overlapping_speaker_ids() -> None:
    segment = TranscriptSegmentResult(
        text="review this turn",
        start_ms=0,
        end_ms=2_000,
        speaker_id=None,
        detected_language="en",
        confidence=None,
        confidence_source="unavailable",
        overlap_group_id=None,
        text_start=0,
        text_end=16,
        source_language="en",
    )
    aligned, warnings = align_diarization_segments(
        [segment],
        [
            DiarizationTurn(0, 1_500, "SPEAKER_00"),
            DiarizationTurn(1_000, 2_000, "SPEAKER_01"),
        ],
    )

    assert aligned[0].speaker_id == "SPEAKER_00"
    assert aligned[0].speaker_ids == ("SPEAKER_00", "SPEAKER_01")
    assert aligned[0].overlap_group_id == "pyannote-overlap-1"
    assert warnings == ("LOCAL_DIARIZATION_OVERLAP_REVIEW",)


def test_remote_audio_45_second_hang_maps_to_typed_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.voice import worker

    class HangingProvider:
        provider_name = "openai"

        async def transcribe(self, _audio_path: Path) -> Any:
            await asyncio.sleep(45)

    monkeypatch.setattr(
        worker,
        "_configured_provider",
        lambda _db, _session: (HangingProvider(), None),
    )
    monkeypatch.setattr(worker, "_assert_audio_circuit_available", lambda *_: None)

    async def immediate_timeout(awaitable: Any, **_kwargs: Any) -> Any:
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(worker.asyncio, "wait_for", immediate_timeout)
    result, code, failure = asyncio.run(
        worker._transcribe(  # noqa: SLF001
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(
                clinic_id=__import__("uuid").uuid4(), synthetic_fixture=False
            ),
            b"RIFF fixture",
        )
    )

    assert result is None
    assert code == "PROVIDER_TIMEOUT"
    assert failure is not None
    assert failure.failure_class == "timeout"
    assert failure.retryable is True
