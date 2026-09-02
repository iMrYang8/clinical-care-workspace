from __future__ import annotations

import json
from pathlib import Path

from trilingual_consult.eval_audio import evaluate_audio
from trilingual_consult.polywer import polywer, tagged_tokens

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "datasets" / "nightingale_switchcare" / "scripts" / "consult-01.json"


def test_tagged_tokens_split_english_island() -> None:
    tokens = tagged_tokens(
        "Dia ada alahan kepada [[EN]] penicillin [[/EN]] masa kecil.",
        matrix_language="ms",
    )
    langs = {token: lang for token, lang in tokens}
    assert langs["penicillin"] == "en"
    assert langs["alahan"] == "ms"
    assert langs["dia"] == "ms"


def test_polywer_tagged_hypothesis_is_per_language() -> None:
    gold = "Dia ada alahan kepada [[EN]] penicillin [[/EN]] masa kecil."
    hyp = "Dia ada alahan kepada [[EN]] penisilin [[/EN]] masa kechil."
    scores = polywer(gold, hyp, matrix_language="ms", hypothesis_tagged=True)
    assert scores["en"].reference_tokens == 1
    assert scores["en"].substitutions == 1
    assert scores["en"].wer == 1.0
    assert scores["ms"].wer is not None
    assert scores["overall"].wer is not None
    assert scores["overall"].wer > 0


def test_untagged_hypothesis_only_reports_overall() -> None:
    gold = "We'll continue [[EN]] metformin 500 mg [[/EN]] twice daily."
    scores = polywer(
        gold,
        "We'll continue metformin 500 mg twice daily.",
        matrix_language="en",
        hypothesis_tagged=False,
    )
    assert set(scores) == {"overall"}
    assert scores["overall"].wer == 0.0


def test_eval_audio_abstains_without_wavs_or_asr(tmp_path: Path) -> None:
    report = evaluate_audio(GOLD, tmp_path / "missing-audio")
    assert report["synthetic"] is True
    assert report["not_clinical_validation"] is True
    assert report["audio_status"] == "TTS_UNAVAILABLE"
    assert "overall_wer" not in report
    assert report["turns"] == []


def test_eval_audio_scores_injected_hypothesis(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "SPEAKER_00.wav").write_bytes(b"RIFF")
    hyp = tmp_path / "hyp.json"
    hyp.write_text(
        json.dumps(
            {
                "model": "unit-hypothesis",
                "turns": [
                    {
                        "speaker_id": "SPEAKER_00",
                        "tagged_text": "We'll continue [[EN]] metformin 500 mg [[/EN]] twice daily.",
                    },
                    {
                        "speaker_id": "SPEAKER_01",
                        "tagged_text": "我对盘尼西林不过敏，是胃不舒服。",
                    },
                    {
                        "speaker_id": "SPEAKER_02",
                        "tagged_text": "Dia ada alahan kepada [[EN]] penicillin [[/EN]] masa kecil.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_audio(GOLD, audio_dir, hypothesis_path=hyp)
    assert report["audio_status"] == "SCORED"
    assert report["asr_model"] == "unit-hypothesis"
    assert report["turns"][0]["scores"]["overall"]["wer"] == 0.0
