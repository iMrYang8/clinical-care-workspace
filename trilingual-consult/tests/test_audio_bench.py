from __future__ import annotations

import json
from pathlib import Path

import pytest

from trilingual_consult.audio_bench import (
    DATASETS,
    BenchClip,
    error_rate,
    known_drug_keys,
    run_named_set,
    score_clip,
    summarise_reports,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_score_clip_without_asr_runs_agents_on_gold_and_has_no_wer() -> None:
    clip = BenchClip(
        dataset="vimedcss",
        clip_id="mock-1",
        transcript="Benh nhan di ung penicillin luc nho.",
        language="vi",
        licence="CC-BY-4.0",
        notes="mock",
    )
    report = score_clip(clip)
    assert report["asr_status"] == "ASR_UNAVAILABLE"
    assert report["wer"] is None
    assert report["hypothesis"] is None
    assert "penicillin" in report["known_drugs_in_gold"]
    assert "penicillin" in report["island_present_in_working"]
    # A positive allergy mention is not a denial, so the weak-denial guard stays
    # silent even though the Vietnamese matrix is unsupported.
    assert report["false_nkda"] is False


def test_unrelated_transcript_does_not_invent_nkda() -> None:
    clip = BenchClip(
        dataset="ascend",
        clip_id="mock-2",
        transcript="今天天气很好 we can go later",
        language="zh",
    )
    report = score_clip(clip)
    assert report["false_nkda"] is False
    assert report["known_drugs_in_gold"] == []


def test_mocked_asr_records_wer_and_model_id() -> None:
    clip = BenchClip(
        dataset="vimedcss",
        clip_id="mock-asr",
        transcript="continue metformin 500 mg",
        language="en",
        audio_path=Path("/tmp/does-not-need-to-exist.wav"),
    )

    def transcribe(_clip: BenchClip) -> tuple[str, str]:
        return "continue metformin 500 mg", "faster-whisper:mock"

    report = score_clip(clip, transcribe=transcribe, char_level=False)
    assert report["asr_status"] == "SCORED"
    assert report["asr_model"] == "faster-whisper:mock"
    assert report["wer"] == 0.0
    assert "metformin" in report["island_canonicalised"]


def test_english_allergy_island_canonicalises_penicillin() -> None:
    clip = BenchClip(
        dataset="vimedcss",
        clip_id="mock-en-island",
        transcript="Patient is allergic to penicillin after the last course.",
        language="en",
    )
    report = score_clip(clip)
    assert "penicillin" in report["island_canonicalised"]
    assert report["false_nkda"] is False


def test_error_rate_char_and_word() -> None:
    assert error_rate("ab cd", "ab cd", char_level=False) == 0.0
    assert error_rate("abcd", "abxd", char_level=True) == 0.25


def test_known_drug_keys_han_and_english() -> None:
    assert "penicillin" in known_drug_keys("我对盘尼西林过敏")
    assert "metformin" in known_drug_keys("We'll continue metformin 500 mg")


def test_run_named_set_uses_injected_clips(tmp_path: Path) -> None:
    clips = [
        BenchClip(
            dataset="vimedcss",
            clip_id="a",
            transcript="Uong amoxicillin moi ngay.",
            language="vi",
        )
    ]
    summary = run_named_set("vimedcss", 40, clips=clips, transcribe=None)
    assert summary["n"] == 1
    assert summary["asr_status"] == "ASR_UNAVAILABLE"
    assert summary["mean_error_rate"] is None
    assert summary["not_claim"]
    assert "Not Malay" in summary["not_claim"]
    out = tmp_path / "vimedcss.json"
    out.write_text(json.dumps(summary), encoding="utf-8")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["clips"][0]["false_nkda"] is False


@pytest.mark.audio
def test_opt_in_hf_stream_is_skipped_by_default() -> None:
    pytest.skip("opt-in live HF stream; run audio_bench CLI instead")


def test_denial_in_an_unsupported_matrix_is_flagged_as_weakly_evidenced() -> None:
    """The weak-denial guard has to actually fire, or it is decoration.

    An English negation pattern can match inside a matrix language the pipeline
    does not support. The resulting fact reports ``en`` and looks trustworthy on
    its own, while nothing ever validated the surrounding grammar. A denial is
    the direction that gets a patient dosed with something they react to, so it
    fails closed.
    """

    clip = BenchClip(
        dataset="vimedcss",
        clip_id="mock-denial",
        transcript="Benh nhan not allergic to penicillin.",
        language="vi",
    )
    report = score_clip(clip)
    assert report["false_nkda"] is True
    assert "BROAD_NKDA_FROM_WEAK_EVIDENCE" in report["warning_codes"]


def test_same_denial_in_a_supported_matrix_is_not_flagged() -> None:
    """The guard must discriminate, not blanket-flag every denial."""

    clip = BenchClip(
        dataset="ascend",
        clip_id="mock-denial-en",
        transcript="Patient is not allergic to penicillin.",
        language="en",
    )
    report = score_clip(clip)
    assert report["false_nkda"] is False
    assert "BROAD_NKDA_FROM_WEAK_EVIDENCE" not in report["warning_codes"]


def test_cs_terms_are_scored_against_the_dataset_own_annotation() -> None:
    """ViMedCSS publishes which terms are code-switched. That label is not ours.

    Scoring survival against somebody else's annotation of somebody else's data
    is the only measurement here that cannot be circular, so it has to be wired
    to the clip's `external` payload rather than re-derived from our lexicon.
    """

    clip = BenchClip(
        dataset="vimedcss",
        clip_id="mock-cs",
        transcript="Benh nhan di ung penicillin va dung metformin moi ngay.",
        language="vi",
        external={"cs_terms_list": "['penicillin', 'metformin', 'paracetamol']"},
    )
    report = score_clip(clip)
    assert report["cs_terms_annotated"] == 3
    assert report["cs_terms_survived"] == 2
    assert report["cs_terms_lost"] == ["paracetamol"]


def test_mixed_turn_detection_is_compared_with_the_declared_label() -> None:
    mixed = BenchClip(
        dataset="ascend",
        clip_id="mock-mixed",
        transcript="今天 we can go later 看医生",
        language="zh",
        external={"language": "mixed"},
    )
    report = score_clip(mixed)
    assert report["declared_mixed"] is True
    assert isinstance(report["detected_mixed"], bool)


def test_summary_omits_external_metrics_when_no_dataset_labels_exist() -> None:
    """A set that publishes no labels must contribute nothing, not a zero.

    A zero would read as a measured failure rather than an absence of evidence.
    """

    clip = BenchClip(
        dataset="multimed",
        clip_id="mock-plain",
        transcript="Patient takes metformin daily.",
        language="en",
    )
    summary = summarise_reports("multimed", DATASETS["multimed"], [score_clip(clip)])
    assert "cs_term_survival" not in summary
    assert "mixed_turn_recall" not in summary
    assert summary["quote_integrity_failures"] == 0
