from __future__ import annotations

import json
from pathlib import Path

import pytest

from trilingual_consult.audio_bench import (
    BenchClip,
    error_rate,
    known_drug_keys,
    run_named_set,
    score_clip,
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
