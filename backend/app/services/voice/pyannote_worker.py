"""Offline subprocess entry point for the optional cached pyannote model."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
from typing import Any

from app.services.voice.ffmpeg import write_private_file


def _turns(pipeline_output: Any) -> list[dict[str, object]]:
    annotation = getattr(pipeline_output, "speaker_diarization", pipeline_output)
    rows: list[dict[str, object]] = []
    for turn, _track, speaker in annotation.itertracks(yield_label=True):
        rows.append(
            {
                "start_ms": int(round(float(turn.start) * 1_000)),
                "end_ms": int(round(float(turn.end) * 1_000)),
                "speaker_id": str(speaker),
                "confidence": None,
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--audio-path", required=True)
    parser.add_argument("--result-path", required=True)
    args = parser.parse_args()
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
        }
    )
    model_dir = Path(args.model_dir).expanduser().resolve()
    audio_path = Path(args.audio_path).resolve()
    result_path = Path(args.result_path).resolve()
    # pyannote is an optional, deployment-provided dependency. Resolve it only
    # inside the isolated worker so importing the core application never
    # requires the heavyweight runtime or a network-backed model install.
    pipeline_type = importlib.import_module("pyannote.audio").Pipeline
    pipeline = pipeline_type.from_pretrained(str(model_dir))
    payload = json.dumps(
        _turns(pipeline(str(audio_path))),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    write_private_file(result_path, payload)


if __name__ == "__main__":
    main()
