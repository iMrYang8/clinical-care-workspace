"""Synthetic-audio PolyWER runner. Abstains unless TTS audio and ASR both exist."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from trilingual_consult.polywer import polywer

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = (
    ROOT / "datasets" / "nightingale_switchcare" / "scripts" / "consult-01.json"
)
DEFAULT_AUDIO = ROOT / "datasets" / "nightingale_switchcare" / "audio" / "consult-01"
DEFAULT_OUT = ROOT / "artifacts" / "polywer-consult-01.json"


def _audio_status(audio_dir: Path) -> str:
    if not audio_dir.is_dir():
        return "TTS_UNAVAILABLE"
    wavs = list(audio_dir.glob("*.wav"))
    if not wavs:
        return "TTS_UNAVAILABLE"
    model_dir = os.environ.get("LOCAL_ASR_MODEL_DIR", "").strip()
    if not model_dir or not Path(model_dir).is_dir():
        return "ASR_UNAVAILABLE"
    return "ASR_READY"


def evaluate_audio(
    gold_path: Path, audio_dir: Path, *, hypothesis_path: Path | None = None
) -> dict[str, Any]:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    status = _audio_status(audio_dir)
    report: dict[str, Any] = {
        "synthetic": True,
        "not_clinical_validation": True,
        "consult_id": gold.get("id"),
        "metric": "polywer",
        "audio_status": status,
        "turns": [],
    }
    if hypothesis_path is None or not hypothesis_path.is_file():
        if status != "ASR_READY":
            return report
        report["audio_status"] = "ASR_UNAVAILABLE"
        report["reason"] = "no hypothesis JSON and no in-process ASR runner"
        return report
    hypotheses = json.loads(hypothesis_path.read_text(encoding="utf-8"))
    by_speaker = {
        str(item["speaker_id"]): item for item in hypotheses.get("turns") or []
    }
    report["audio_status"] = "SCORED"
    report["asr_model"] = hypotheses.get("model")
    for turn in gold.get("turns") or []:
        speaker = str(turn["speaker_id"])
        hyp_turn = by_speaker.get(speaker)
        if hyp_turn is None:
            report["turns"].append(
                {"speaker_id": speaker, "status": "HYPOTHESIS_MISSING"}
            )
            continue
        tagged = bool(hyp_turn.get("tagged_text"))
        scores = polywer(
            str(turn.get("tagged_text") or turn["text"]),
            str(hyp_turn.get("tagged_text") or hyp_turn.get("text") or ""),
            matrix_language=str(turn.get("source_language") or "matrix"),
            hypothesis_tagged=tagged,
        )
        report["turns"].append(
            {
                "speaker_id": speaker,
                "source_language": turn.get("source_language"),
                "scores": {
                    key: {
                        "wer": value.wer,
                        "substitutions": value.substitutions,
                        "deletions": value.deletions,
                        "insertions": value.insertions,
                        "reference_tokens": value.reference_tokens,
                    }
                    for key, value in scores.items()
                },
            }
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score consult-01 synthetic audio with PolyWER, or abstain."
    )
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--hypothesis", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    report = evaluate_audio(args.gold, args.audio_dir, hypothesis_path=args.hypothesis)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"audio_status": report["audio_status"], "path": str(args.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
