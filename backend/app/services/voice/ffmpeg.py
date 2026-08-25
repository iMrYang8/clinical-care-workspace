from __future__ import annotations

import array
import hashlib
import math
import os
import stat
import subprocess
import sys
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
    source_path: Path | None = None


@dataclass(frozen=True)
class PreprocessedAudio:
    payload: bytes
    sha256: str
    duration_ms: int
    sample_rate_hz: int
    channels: int
    signals: dict[str, object]


_PCM_ANALYSIS_FRAMES = 65_536
_DEFAULT_MAX_DURATION_MS = 60 * 60 * 1_000
_DEFAULT_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_OUTPUT_LIMIT_MARGIN_BYTES = 64 * 1024
_INPUT_DEMUXERS = {
    "audio/webm": "matroska,webm",
    "audio/mp4": "mov,mp4,m4a,3gp,3g2,mj2",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


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


def _input_args(media_type: str, path: Path) -> list[str]:
    normalized = media_type.split(";", 1)[0].strip().lower()
    demuxer = _INPUT_DEMUXERS.get(normalized)
    if demuxer is None:
        raise AudioPreprocessingError("AUDIO_MEDIA_TYPE_INVALID")
    # Fixed demuxer + file-only nested protocols prevents a disguised HLS,
    # concat, or other playlist from turning authenticated audio into SSRF or
    # local-file reads. FFmpeg still validates the bytes against the demuxer.
    return [
        "-protocol_whitelist",
        "file",
        "-f",
        demuxer,
        "-i",
        str(path),
    ]


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


def _pcm_signals(
    path: Path,
    *,
    multi_device: bool,
    max_duration_ms: int = _DEFAULT_MAX_DURATION_MS,
) -> tuple[int, dict[str, object]]:
    with wave.open(str(path), "rb") as stream:
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
        frame_count = stream.getnframes()
        if sample_width != 2 or channels != 1 or sample_rate != 16_000:
            raise AudioPreprocessingError("FFMPEG_OUTPUT_FORMAT_INVALID")
        max_frames = max(1, max_duration_ms) * sample_rate // 1_000
        if frame_count > max_frames:
            raise AudioPreprocessingError("AUDIO_DECODE_LIMIT_EXCEEDED")
        sample_count = 0
        silence_count = 0
        clipping_count = 0
        square_sum = 0
        while payload := stream.readframes(_PCM_ANALYSIS_FRAMES):
            samples = array.array("h")
            samples.frombytes(payload)
            if sys.byteorder != "little":
                samples.byteswap()
            sample_count += len(samples)
            for value in samples:
                magnitude = abs(value)
                silence_count += magnitude < 500
                clipping_count += magnitude >= 32_700
                square_sum += value * value
    duration_ms = int(round(frame_count * 1_000 / sample_rate))
    count = max(1, sample_count)
    silence_ratio = silence_count / count
    clipping_ratio = clipping_count / count
    rms = math.sqrt(square_sum / count)
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


def _run_ffmpeg(
    command: list[str],
    output: Path,
    *,
    timeout_seconds: int,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> None:
    # ``-fs`` is placed immediately before the sole output path. It limits fast
    # decompression bombs even when the process finishes before the wall timeout.
    bounded_command = [
        *command[:-1],
        "-fs",
        str(max(1, max_output_bytes)),
        command[-1],
    ]
    try:
        completed = subprocess.run(
            bounded_command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
    # FFmpeg may return zero after ``-fs`` truncates a container a few muxing
    # bytes below the requested cap. Reject the safety margin as well; evidence
    # is never accepted merely because a truncated WAV is syntactically valid.
    truncation_threshold = max(
        1, max_output_bytes - min(_OUTPUT_LIMIT_MARGIN_BYTES, max_output_bytes // 8)
    )
    if output.stat().st_size >= truncation_threshold:
        raise AudioPreprocessingError("AUDIO_DECODE_LIMIT_EXCEEDED")
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
    max_duration_ms: int = _DEFAULT_MAX_DURATION_MS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
) -> PreprocessedAudio:
    """Validate, assemble, and normalize device tracks to 16 kHz mono PCM."""

    if not devices:
        raise AudioPreprocessingError("NO_AUDIO_CHUNKS")
    max_pcm_payload = max(1, max_duration_ms) * 16_000 * 2 // 1_000
    if max_output_bytes <= max_pcm_payload + _OUTPUT_LIMIT_MARGIN_BYTES:
        raise AudioPreprocessingError("AUDIO_LIMIT_CONFIGURATION_INVALID")
    with tempfile.TemporaryDirectory(prefix="nightingale-voice-") as temp_name:
        temp_dir = Path(temp_name)
        os.chmod(temp_dir, stat.S_IRWXU)
        inputs: list[Path] = []
        for index, device in enumerate(devices):
            if device.source_path is not None:
                path = device.source_path
                if not path.is_file():
                    raise AudioPreprocessingError("AUDIO_SOURCE_FILE_MISSING")
            else:
                path = temp_dir / f"device-{index}{_suffix(device.media_type)}"
                write_private_file(path, _ordered_device_payload(device))
            inputs.append(path)

        # Measure each decoded track before highpass/loudnorm.  Otherwise
        # normalization can conceal source clipping or amplify low-level noise.
        raw_signals: list[dict[str, object]] = []
        raw_filter = "aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono"
        for index, (device, path) in enumerate(zip(devices, inputs, strict=True)):
            analysis_output = temp_dir / f"analysis-{index}.wav"
            analysis_command = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                *_input_args(device.media_type, path),
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
                max_output_bytes=max_output_bytes,
            )
            _duration, device_signals = _pcm_signals(
                analysis_output,
                multi_device=len(inputs) > 1,
                max_duration_ms=max_duration_ms,
            )
            raw_signals.append(device_signals)

        output = temp_dir / "normalized.wav"
        command = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        for device, path in zip(devices, inputs, strict=True):
            command.extend(_input_args(device.media_type, path))
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
        _run_ffmpeg(
            command,
            output,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        payload = output.read_bytes()
        duration_ms, normalized_signals = _pcm_signals(
            output,
            multi_device=len(inputs) > 1,
            max_duration_ms=max_duration_ms,
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
