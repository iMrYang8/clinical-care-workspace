#!/usr/bin/env python3
"""Fail-closed binding checks for the final English-captioned demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--sha-file", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-image", required=True)
    args = parser.parse_args()

    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    expected = {
        "runtime_git_revision": args.expected_commit,
        "oci_image_digest": args.expected_image,
        "language": "en",
        "narration": False,
        "subtitles": "burned-in+sidecar",
        "output_resolution": "1920x1080",
        "audio_streams": 0,
    }
    mismatches = {
        key: {"expected": value, "actual": metadata.get(key)}
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if mismatches:
        raise SystemExit(f"Demo metadata binding mismatch: {mismatches}")

    video_hash = sha256(args.video)
    srt_hash = sha256(args.srt)
    if metadata.get("video_sha256") != video_hash:
        raise SystemExit("Demo video SHA-256 does not match metadata")
    if metadata.get("srt_sha256") != srt_hash:
        raise SystemExit("Demo SRT SHA-256 does not match metadata")

    subprocess.run(
        ["shasum", "-a", "256", "-c", args.sha_file.name],
        cwd=args.sha_file.parent,
        check=True,
    )
    probe = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,avg_frame_rate",
                "-of",
                "json",
                str(args.video),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    videos = [stream for stream in probe["streams"] if stream["codec_type"] == "video"]
    audios = [stream for stream in probe["streams"] if stream["codec_type"] == "audio"]
    if len(videos) != 1 or audios:
        raise SystemExit("Final demo must contain one video stream and no audio streams")
    video = videos[0]
    if (
        video.get("codec_name") != "h264"
        or video.get("width") != 1920
        or video.get("height") != 1080
        or video.get("avg_frame_rate") != "30/1"
    ):
        raise SystemExit(f"Unexpected final demo video stream: {video}")

    print(
        json.dumps(
            {
                "status": "passed",
                "runtime_git_revision": args.expected_commit,
                "oci_image_digest": args.expected_image,
                "video_sha256": video_hash,
                "srt_sha256": srt_hash,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
