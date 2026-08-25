from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.voice.ffmpeg import write_private_file
from app.services.voice.providers.local_whisper import (
    result_payload,
    transcribe_local_sync,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--audio-path", required=True)
    parser.add_argument("--result-path", required=True)
    args = parser.parse_args()
    try:
        result = transcribe_local_sync(
            Path(args.model_dir).resolve(), Path(args.audio_path).resolve()
        )
        payload = json.dumps(
            result_payload(result), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        write_private_file(Path(args.result_path).resolve(), payload)
    except Exception:
        # The parent exposes a stable non-PHI error code and never logs model
        # stderr or transcript fragments from this optional process.
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
