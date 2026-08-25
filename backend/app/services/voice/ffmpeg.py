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
            path.write_bytes(_ordered_device_payload(device))
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            inputs.append(path)

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
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=max(1, timeout_seconds),
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
        payload = output.read_bytes()
        duration_ms, signals = _pcm_signals(output, multi_device=len(inputs) > 1)
        return PreprocessedAudio(
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            duration_ms=duration_ms,
            sample_rate_hz=16_000,
            channels=1,
            signals=signals,
        )
