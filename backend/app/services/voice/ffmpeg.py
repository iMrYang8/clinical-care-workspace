from __future__ import annotations

import hashlib
import math
import os
import stat
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path


class AudioPreprocessingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DeviceAudio:
    device_id: str
    media_type: str
    chunks: list[tuple[int, bytes, str]]


@dataclass(frozen=True)
class PreprocessedAudio:
    payload: bytes
    sha256: str
    duration_ms: int
    sample_rate_hz: int
    channels: int
    signals: dict[str, object]


def write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def _suffix(media_type: str) -> str:
    normalized = media_type.split(";", 1)[0].strip().lower()
    return {
        "audio/webm": ".webm",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }.get(normalized, ".audio")


def _ordered_device_payload(device: DeviceAudio) -> bytes:
    ordered = sorted(device.chunks, key=lambda item: item[0])
    expected = list(range(len(ordered)))
    if [item[0] for item in ordered] != expected:
        raise AudioPreprocessingError("AUDIO_CHUNK_SEQUENCE_INVALID")
    payloads: list[bytes] = []
    for _index, payload, digest in ordered:
        if hashlib.sha256(payload).hexdigest() != digest:
            raise AudioPreprocessingError("AUDIO_CHUNK_HASH_INVALID")
        payloads.append(payload)
    return b"".join(payloads)


def _pcm_signals(path: Path, *, multi_device: bool) -> tuple[int, dict[str, object]]:
    with wave.open(str(path), "rb") as stream:
        frames = stream.readframes(stream.getnframes())
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
        frame_count = stream.getnframes()
    if sample_width != 2 or channels != 1 or sample_rate != 16_000:
        raise AudioPreprocessingError("FFMPEG_OUTPUT_FORMAT_INVALID")
    duration_ms = int(round(frame_count * 1_000 / sample_rate)) if sample_rate else 0
    samples = struct.unpack(f"<{len(frames) // 2}h", frames) if frames else ()
    count = max(1, len(samples))
    silence_ratio = sum(abs(value) < 500 for value in samples) / count
    clipping_ratio = sum(abs(value) >= 32_700 for value in samples) / count
    rms = math.sqrt(sum(value * value for value in samples) / count)
    signals: dict[str, object] = {
        "silence_ratio": round(silence_ratio, 6),
        "clipping_ratio": round(clipping_ratio, 6),
        "rms": round(rms, 2),
        "silence_review": silence_ratio > 0.85,
        "clipping_review": clipping_ratio > 0.001,
        "noise_review": 0 < rms < 150,
        # This is a review signal for concurrent device tracks, not blind-source
        # separation or proof that two people spoke simultaneously.
        "overlap_review": multi_device,
        "alignment": "track-start-only" if multi_device else "single-device",
    }
    return duration_ms, signals


def _run_ffmpeg(command: list[str], output: Path, *, timeout_seconds: int) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=max(1, timeout_seconds),
            umask=0o077,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        code = (
            "FFMPEG_TIMEOUT"
            if isinstance(exc, subprocess.TimeoutExpired)
            else "FFMPEG_UNAVAILABLE"
        )
        raise AudioPreprocessingError(code) from exc
    if completed.returncode != 0 or not output.is_file():
        raise AudioPreprocessingError("FFMPEG_PREPROCESSING_FAILED")
    os.chmod(output, stat.S_IRUSR | stat.S_IWUSR)


def _numeric_signal(signals: dict[str, object], key: str) -> float:
    value = signals.get(key)
    if not isinstance(value, (int, float)):
        raise AudioPreprocessingError("AUDIO_SIGNAL_SCHEMA_INVALID")
    return float(value)


def preprocess_audio(
    devices: list[DeviceAudio],
    *,
    ffmpeg_bin: str,
    timeout_seconds: int,
) -> PreprocessedAudio:
    """Validate, assemble, and normalize device tracks to 16 kHz mono PCM."""

    if not devices:
        raise AudioPreprocessingError("NO_AUDIO_CHUNKS")
    with tempfile.TemporaryDirectory(prefix="nightingale-voice-") as temp_name:
        temp_dir = Path(temp_name)
        os.chmod(temp_dir, stat.S_IRWXU)
        inputs: list[Path] = []
        for index, device in enumerate(devices):
            path = temp_dir / f"device-{index}{_suffix(device.media_type)}"
            write_private_file(path, _ordered_device_payload(device))
            inputs.append(path)

        # Measure each decoded track before highpass/loudnorm.  Otherwise
        # normalization can conceal source clipping or amplify low-level noise.
        raw_signals: list[dict[str, object]] = []
        raw_filter = "aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono"
        for index, path in enumerate(inputs):
            analysis_output = temp_dir / f"analysis-{index}.wav"
            analysis_command = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(path),
                "-af",
                raw_filter,
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(analysis_output),
            ]
            _run_ffmpeg(
                analysis_command,
                analysis_output,
                timeout_seconds=timeout_seconds,
            )
            _duration, device_signals = _pcm_signals(
                analysis_output, multi_device=len(inputs) > 1
            )
            raw_signals.append(device_signals)

        output = temp_dir / "normalized.wav"
        command = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        for path in inputs:
            command.extend(["-i", str(path)])
        base_filter = (
            "aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono,highpass=f=80"
        )
        if len(inputs) == 1:
            command.extend(
                [
                    "-af",
                    f"{base_filter},loudnorm=I=-16:LRA=11:TP=-1.5",
                ]
            )
        else:
            tracks = ";".join(
                f"[{index}:a]{base_filter}[a{index}]" for index in range(len(inputs))
            )
            labels = "".join(f"[a{index}]" for index in range(len(inputs)))
            command.extend(
                [
                    "-filter_complex",
                    f"{tracks};{labels}amix=inputs={len(inputs)}:duration=longest:normalize=0,"
                    "loudnorm=I=-16:LRA=11:TP=-1.5[out]",
                    "-map",
                    "[out]",
                ]
            )
        command.extend(["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(output)])
        _run_ffmpeg(command, output, timeout_seconds=timeout_seconds)
        payload = output.read_bytes()
        duration_ms, normalized_signals = _pcm_signals(
            output, multi_device=len(inputs) > 1
        )
        signals: dict[str, object] = {
            "silence_ratio": max(
                _numeric_signal(item, "silence_ratio") for item in raw_signals
            ),
            "clipping_ratio": max(
                _numeric_signal(item, "clipping_ratio") for item in raw_signals
            ),
            "rms": min(_numeric_signal(item, "rms") for item in raw_signals),
            "silence_review": any(
                item["silence_review"] is True for item in raw_signals
            ),
            "clipping_review": any(
                item["clipping_review"] is True for item in raw_signals
            ),
            "noise_review": any(item["noise_review"] is True for item in raw_signals),
            "overlap_review": len(inputs) > 1,
            "alignment": "track-start-only" if len(inputs) > 1 else "single-device",
            "measurement_stage": "decoded-pre-normalization",
            "device_signals": raw_signals,
            "normalized_output_signals": normalized_signals,
        }
        return PreprocessedAudio(
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            duration_ms=duration_ms,
            sample_rate_hz=16_000,
            channels=1,
            signals=signals,
        )
