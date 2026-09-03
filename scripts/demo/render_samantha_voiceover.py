#!/usr/bin/env python3
"""Generate cue-aligned macOS `say` narration and mux it into a demo video.

Every cue is spoken separately, fitted inside its own subtitle window, and laid
down at that window's start, so the narration and the SRT cannot drift apart.
Used for the twelve-minute demo (`--expect-duration 720`) and for each
per-scenario recording via `voice_scenarios.sh`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Cue:
    index: int
    start: float
    end: float
    text: str


TIME_RE = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})"
    r"\s+-->\s+"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})"
)


def seconds(hours: str, minutes: str, secs: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis) / 1000


def parse_srt(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        match = TIME_RE.fullmatch(lines[1])
        if match is None:
            raise ValueError(f"Invalid SRT timing block: {block}")
        groups = match.groupdict()
        cues.append(
            Cue(
                index=int(lines[0]),
                start=seconds(groups["sh"], groups["sm"], groups["ss"], groups["sms"]),
                end=seconds(groups["eh"], groups["em"], groups["es"], groups["ems"]),
                text=" ".join(lines[2:]),
            )
        )
    if not cues or any(right.start <= left.start for left, right in zip(cues, cues[1:])):
        raise ValueError("SRT cues must be non-empty and strictly ordered")
    return cues


def run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        text=True,
        capture_output=capture,
    )


def probe(path: Path) -> dict:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,channels,sample_rate",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atempo_chain(ratio: float) -> str:
    factors: list[float] = []
    while ratio > 2:
        factors.append(2.0)
        ratio /= 2
    while ratio < 0.5:
        factors.append(0.5)
        ratio /= 0.5
    factors.append(ratio)
    return ",".join(f"atempo={factor:.8f}" for factor in factors)


def synthesize_cue(
    cue: Cue,
    *,
    voice: str,
    rate: int,
    cache: Path,
    sample_rate: int,
) -> tuple[Path, float, float]:
    aiff = cache / f"cue-{cue.index:03d}.aiff"
    wav = cache / f"cue-{cue.index:03d}.wav"
    if not aiff.exists() or duration(aiff) <= 0:
        run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff), cue.text])
    source_duration = duration(aiff)
    target_duration = max(0.25, cue.end - cue.start - 0.08)
    ratio = max(1.0, source_duration / target_duration)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff)]
    if ratio > 1.0005:
        command.extend(["-af", atempo_chain(ratio)])
    command.extend(
        [
            "-t",
            f"{target_duration:.6f}",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(wav),
        ]
    )
    run(command)
    return wav, source_duration, ratio


def assemble_track(
    cues: list[Cue],
    clips: list[Path],
    *,
    output: Path,
    total_duration: float,
    sample_rate: int,
) -> None:
    frame_count = round(total_duration * sample_rate)
    pcm = bytearray(frame_count * 2)
    for cue, clip in zip(cues, clips):
        with wave.open(str(clip), "rb") as stream:
            if (
                stream.getnchannels() != 1
                or stream.getsampwidth() != 2
                or stream.getframerate() != sample_rate
            ):
                raise ValueError(f"Unexpected cue format: {clip}")
            frames = stream.readframes(stream.getnframes())
        start = round(cue.start * sample_rate) * 2
        end = min(len(pcm), start + len(frames))
        pcm[start:end] = frames[: end - start]
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice", default="Samantha")
    parser.add_argument("--rate", type=int, default=220)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument(
        "--subtitle-track",
        default="burned in source video",
        help=(
            "How the viewer gets these cues as text. The twelve-minute demo has "
            "them burned in; the per-scenario clips ship the SRT beside them."
        ),
    )
    parser.add_argument(
        "--expect-duration",
        type=float,
        default=None,
        help=(
            "Refuse to render unless the input video is this many seconds long. "
            "Pass 720 for the twelve-minute demo; the per-scenario clips each "
            "declare their own length in scenario_narration.mjs."
        ),
    )
    parser.add_argument("--narration-audio", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--sha-file", type=Path, required=True)
    args = parser.parse_args()

    for command in ("say", "ffmpeg", "ffprobe"):
        if shutil.which(command) is None:
            raise RuntimeError(f"Required command is missing: {command}")

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cues = parse_srt(args.srt)
    video_probe = probe(args.video)
    video_duration = float(video_probe["format"]["duration"])
    if (
        args.expect_duration is not None
        and abs(video_duration - args.expect_duration) > 0.12
    ):
        raise ValueError(
            f"Expected a {args.expect_duration}-second input video, found {video_duration}"
        )
    if cues[-1].end > video_duration + 0.001:
        raise ValueError(
            f"Final cue ends at {cues[-1].end}s, past the {video_duration}s video"
        )

    clips: list[Path] = []
    ratios: list[float] = []
    source_durations: list[float] = []
    for position, cue in enumerate(cues, start=1):
        print(f"[{position}/{len(cues)}] {args.voice} cue {cue.index}", flush=True)
        clip, source_duration, ratio = synthesize_cue(
            cue,
            voice=args.voice,
            rate=args.rate,
            cache=args.cache_dir,
            sample_rate=48_000,
        )
        clips.append(clip)
        source_durations.append(source_duration)
        ratios.append(ratio)

    narration_wav = args.cache_dir / "narration.wav"
    assemble_track(
        cues,
        clips,
        output=narration_wav,
        total_duration=video_duration,
        sample_rate=48_000,
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(narration_wav),
            "-af",
            "loudnorm=I=-17:TP=-2:LRA=7",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(args.narration_audio),
        ]
    )

    temporary_output = args.output.with_suffix(".tmp.mp4")
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(args.video),
            "-i",
            str(args.narration_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-t",
            f"{video_duration:.6f}",
            "-movflags",
            "+faststart",
            str(temporary_output),
        ]
    )
    temporary_output.replace(args.output)

    final_probe = probe(args.output)
    streams = final_probe["streams"]
    video_streams = [stream for stream in streams if stream["codec_type"] == "video"]
    audio_streams = [stream for stream in streams if stream["codec_type"] == "audio"]
    final_duration = float(final_probe["format"]["duration"])
    if (
        len(video_streams) != 1
        or len(audio_streams) != 1
        or video_streams[0]["codec_name"] != "h264"
        or audio_streams[0]["codec_name"] != "aac"
        or audio_streams[0].get("sample_rate") != "48000"
        or audio_streams[0].get("channels") != 2
        or abs(final_duration - video_duration) > 0.12
    ):
        raise ValueError(f"Unexpected voiced demo probe: {json.dumps(final_probe)}")

    metadata = {
        "language": "en",
        "narration": True,
        "narration_voice": args.voice,
        "narration_engine": "macOS say",
        "requested_rate": args.rate,
        "alignment": "SRT cue start; every cue fitted within its subtitle window",
        "cue_count": len(cues),
        "duration_seconds": final_duration,
        "video_codec": video_streams[0]["codec_name"],
        "audio_codec": audio_streams[0]["codec_name"],
        "audio_sample_rate": int(audio_streams[0]["sample_rate"]),
        "audio_channels": audio_streams[0]["channels"],
        "max_tempo_ratio": max(ratios),
        "tempo_adjusted_cues": sum(ratio > 1.0005 for ratio in ratios),
        "source_spoken_seconds": sum(source_durations),
        "source_video": args.video.name,
        "source_video_sha256": sha256(args.video),
        "srt": args.srt.name,
        "srt_sha256": sha256(args.srt),
        "narration_audio": args.narration_audio.name,
        "narration_audio_sha256": sha256(args.narration_audio),
        "output": args.output.name,
        "output_sha256": sha256(args.output),
        "qa": {
            "cue_alignment": "passed",
            "duration": "passed",
            "one_h264_video_stream": "passed",
            "one_aac_audio_stream": "passed",
            "subtitle_track": args.subtitle_track,
        },
    }
    args.metadata.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    files = [args.output, args.narration_audio, args.srt, args.metadata]
    args.sha_file.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in files),
        encoding="utf-8",
    )
    print(f"VOICED_VIDEO={args.output}")
    print(f"NARRATION_AUDIO={args.narration_audio}")
    print(f"VOICEOVER_METADATA={args.metadata}")
    print(f"VOICEOVER_SHA256={args.sha_file}")


if __name__ == "__main__":
    main()
