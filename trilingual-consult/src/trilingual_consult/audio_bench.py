"""Downloadable multilingual audio bench. Not a SEA trilingual consult score.

Public ms/en/nan medical consult audio does not exist. This runner scores
small Hugging Face slices (ViMedCSS, ASCEND, MultiMed, Common Voice nan-tw)
when they can be streamed, and abstains when ASR or the dataset is missing.
It does not download full dumps, does not pirate gated sets, and does not
average a Vietnamese WER into a Q6 Malay–Hokkien number.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import wave
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trilingual_consult.lexicon import DRUG_ALIASES, canonicalize_drug
from trilingual_consult.pipeline import run_consult_pipeline
from trilingual_consult.polywer import _levenshtein
from trilingual_consult.state import ConsultInput, ConsultTurn

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "artifacts" / "audio-bench"
CACHE_DIR = ROOT / "datasets" / "external" / "cache"

_KNOWN_DRUG = re.compile(
    r"\b(?:penicillin|penisilin|amoxicillin|amoksisilin|metformin|aspirin|"
    r"paracetamol|ibuprofen|warfarin|insulin)\b",
    re.I,
)


@dataclass(frozen=True)
class BenchClip:
    dataset: str
    clip_id: str
    transcript: str
    language: str | None = None
    audio_path: Path | None = None
    licence: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    hf_id: str
    split: str
    language: str | None
    licence: str
    proves: str
    not_claim: str
    config: str | None = None
    gated: bool = False
    char_level: bool = False


DATASETS: dict[str, DatasetSpec] = {
    "vimedcss": DatasetSpec(
        name="vimedcss",
        hf_id="tensorxt/ViMedCSS",
        split="test",
        language="vi",
        licence="CC-BY-4.0",
        proves="Vietnamese medical matrix + English term islands (ViMedCSS).",
        not_claim="Not Malay, not Hokkien, not a Singapore consult.",
        char_level=True,
    ),
    "ascend": DatasetSpec(
        name="ascend",
        hf_id="CAiRE/ASCEND",
        split="test",
        language="zh",
        licence="CC-BY-SA",
        proves="Spontaneous Mandarin–English intra-utterance code-switching.",
        not_claim="Not medical consult speech and not SEA trilingual.",
        char_level=True,
    ),
    "multimed": DatasetSpec(
        name="multimed",
        hf_id="leduckhai/MultiMed",
        split="test",
        language="vi",
        licence="dataset card",
        proves="Multilingual medical ASR (vi/en/de/fr/zh), not necessarily CS.",
        not_claim="Not a Malay–English–Hokkien consult set.",
        char_level=True,
    ),
    "nan-tw": DatasetSpec(
        name="nan-tw",
        hf_id="mozilla-foundation/common_voice_17_0",
        split="validation",
        language="nan",
        licence="CC0",
        proves="Read Taiwanese Hokkien smoke (Common Voice nan-tw).",
        not_claim="Taiwanese ≠ Singapore Hokkien. Not medical consult audio.",
        config="nan-TW",
        char_level=True,
    ),
    "afriswitchcare": DatasetSpec(
        name="afriswitchcare",
        hf_id="intronhealth/AfriSwitchCare",
        split="train",
        language="en",
        licence="CC-BY-NC-SA (gated)",
        proves="Simulated CS consults with [[EN]] tags (same recipe as our gold).",
        not_claim="Gated. Skip if Hugging Face returns 401. Do not scrape.",
        gated=True,
    ),
}


def known_drug_keys(text: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for match in _KNOWN_DRUG.finditer(text):
        key = canonicalize_drug(match.group(0)) or match.group(0).casefold()
        if key not in seen:
            seen.add(key)
            keys.append(key)
    for canonical, aliases in DRUG_ALIASES.items():
        if canonical in seen:
            continue
        if any(alias in text for alias in aliases if not alias.isascii()):
            seen.add(canonical)
            keys.append(canonical)
    return keys


def error_rate(reference: str, hypothesis: str, *, char_level: bool) -> float | None:
    if char_level:
        ref = [ch for ch in reference if not ch.isspace()]
        hyp = [ch for ch in hypothesis if not ch.isspace()]
    else:
        ref = reference.casefold().split()
        hyp = hypothesis.casefold().split()
    if not ref:
        return None
    substitutions, deletions, insertions = _levenshtein(ref, hyp)
    return round((substitutions + deletions + insertions) / len(ref), 4)


def asr_status() -> str:
    model_dir = os.environ.get("LOCAL_ASR_MODEL_DIR", "").strip()
    if not model_dir or not Path(model_dir).is_dir():
        return "ASR_UNAVAILABLE"
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return "ASR_UNAVAILABLE"
    return "ASR_READY"


def score_clip(
    clip: BenchClip,
    *,
    transcribe: Callable[[BenchClip], tuple[str, str]] | None = None,
    char_level: bool = False,
) -> dict[str, Any]:
    """Run agents on ASR hyp if present, else gold transcript. Never invent WER."""

    report: dict[str, Any] = {
        "dataset": clip.dataset,
        "clip_id": clip.clip_id,
        "synthetic": False,
        "not_clinical_validation": True,
        "licence": clip.licence,
        "notes": clip.notes,
        "gold_transcript": clip.transcript,
        "asr_status": "ASR_UNAVAILABLE",
        "hypothesis": None,
        "asr_model": None,
        "wer": None,
        "error_rate_unit": "char" if char_level else "word",
        "known_drugs_in_gold": known_drug_keys(clip.transcript),
        "false_nkda": False,
    }
    working = clip.transcript
    if transcribe is not None:
        hypothesis, model_id = transcribe(clip)
        report["asr_status"] = "SCORED"
        report["hypothesis"] = hypothesis
        report["asr_model"] = model_id
        report["wer"] = error_rate(clip.transcript, hypothesis, char_level=char_level)
        working = hypothesis
    state = run_consult_pipeline(
        ConsultInput(
            consult_id=f"{clip.dataset}-{clip.clip_id}",
            turns=[
                ConsultTurn(
                    speaker_id="SPEAKER_00",
                    text=working,
                    source_language=clip.language,
                )
            ],
        )
    )
    allergies = [
        fact for fact in state.proposed_facts if fact.fact_type == "allergy"
    ]
    nkda = [
        fact
        for fact in allergies
        if fact.polarity == "absent" and fact.key in {"*", ""}
    ]
    report["false_nkda"] = bool(nkda)
    report["allergy_keys"] = sorted({fact.key for fact in allergies if fact.key != "*"})
    working_fold = working.casefold()
    report["island_present_in_working"] = [
        key
        for key in report["known_drugs_in_gold"]
        if key in working_fold
        or any(alias in working for alias in DRUG_ALIASES.get(key, ()))
    ]
    report["island_canonicalised"] = [
        key
        for key in report["known_drugs_in_gold"]
        if key in report["allergy_keys"]
        or any(
            fact.key == key
            for fact in state.proposed_facts
            if fact.fact_type in {"medication", "allergy"}
        )
    ]
    report["warning_codes"] = list(state.warning_codes)
    report["review_required"] = any(fact.review_required for fact in state.proposed_facts)
    return report


def summarise_reports(name: str, spec: DatasetSpec, reports: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [item for item in reports if item.get("asr_status") == "SCORED"]
    wers = [item["wer"] for item in scored if item.get("wer") is not None]
    return {
        "dataset": name,
        "hf_id": spec.hf_id,
        "licence": spec.licence,
        "proves": spec.proves,
        "not_claim": spec.not_claim,
        "synthetic": False,
        "not_clinical_validation": True,
        "n": len(reports),
        "n_asr_scored": len(scored),
        "asr_status": "SCORED" if scored else "ASR_UNAVAILABLE",
        "mean_error_rate": round(sum(wers) / len(wers), 4) if wers else None,
        "false_nkda_count": sum(1 for item in reports if item.get("false_nkda")),
        "island_hits": sum(1 for item in reports if item.get("island_canonicalised")),
        "clips": reports,
    }


def _transcript_from_row(row: dict[str, Any]) -> str:
    for key in (
        "transcription",
        "transcript",
        "sentence",
        "text",
        "normalized_text",
        "raw_text",
    ):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pcm16_wav(path: Path, samples: Any, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = [int(max(-1.0, min(1.0, float(item))) * 32767) for item in samples]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(int(item).to_bytes(2, "little", signed=True) for item in values))


def _audio_path_from_row(row: dict[str, Any], dest: Path) -> Path | None:
    audio = row.get("audio")
    if isinstance(audio, str) and Path(audio).is_file():
        return Path(audio)
    if not isinstance(audio, dict):
        return None
    nested = audio.get("path")
    if isinstance(nested, str) and Path(nested).is_file():
        return Path(nested)
    array = audio.get("array")
    rate = audio.get("sampling_rate") or audio.get("sample_rate") or 16_000
    if array is None:
        return None
    _pcm16_wav(dest, array, int(rate))
    return dest


def _row_without_audio(row: Any) -> dict[str, Any]:
    if hasattr(row, "keys"):
        keys = [key for key in row.keys() if key != "audio"]
        return {key: row[key] for key in keys}
    if isinstance(row, dict):
        return {key: value for key, value in row.items() if key != "audio"}
    return dict(row)


def iter_hf_rows(spec: DatasetSpec, n: int, *, need_audio: bool = False) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    kwargs: dict[str, Any] = {"split": spec.split, "streaming": True}
    if spec.config:
        kwargs["name"] = spec.config
    dataset = load_dataset(spec.hf_id, **kwargs)
    for index, row in enumerate(dataset):
        if index >= n:
            break
        yield dict(row) if need_audio else _row_without_audio(row)


def clips_from_rows(
    spec: DatasetSpec,
    rows: Iterable[dict[str, Any]],
    *,
    cache_dir: Path = CACHE_DIR,
    need_audio: bool = False,
) -> list[BenchClip]:
    clips: list[BenchClip] = []
    for index, row in enumerate(rows):
        transcript = _transcript_from_row(row)
        if not transcript:
            continue
        clip_id = str(row.get("id") or row.get("path") or index)
        dest = cache_dir / spec.name / f"{index:04d}.wav"
        audio_path = _audio_path_from_row(row, dest) if need_audio else None
        clips.append(
            BenchClip(
                dataset=spec.name,
                clip_id=clip_id,
                transcript=transcript,
                language=spec.language,
                audio_path=audio_path,
                licence=spec.licence,
                notes=spec.not_claim,
            )
        )
    return clips


def try_load_clips(spec: DatasetSpec, n: int) -> tuple[list[BenchClip], str | None]:
    try:
        rows = list(iter_hf_rows(spec, n, need_audio=asr_status() == "ASR_READY"))
    except ImportError:
        return [], "DATASETS_LIB_MISSING"
    except Exception as exc:  # noqa: BLE001 — record gated/network failures honestly
        message = str(exc)
        lowered = message.lower()
        if spec.gated or "401" in message or "gated" in lowered or "restricted" in lowered:
            return [], f"HF_GATED_OR_DENIED:{type(exc).__name__}"
        return [], f"HF_LOAD_FAILED:{type(exc).__name__}"
    if not rows:
        return [], "HF_EMPTY_SPLIT"
    return clips_from_rows(spec, rows, need_audio=asr_status() == "ASR_READY"), None


def make_transcribe() -> Callable[[BenchClip], tuple[str, str]] | None:
    if asr_status() != "ASR_READY":
        return None
    model_dir = os.environ["LOCAL_ASR_MODEL_DIR"].strip()
    from faster_whisper import WhisperModel

    model = WhisperModel(model_dir, device="cpu", compute_type="int8", local_files_only=True)

    def _run(clip: BenchClip) -> tuple[str, str]:
        if clip.audio_path is None or not clip.audio_path.is_file():
            raise FileNotFoundError("clip has no wav")
        segments, _info = model.transcribe(str(clip.audio_path), vad_filter=False)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text, f"faster-whisper:{Path(model_dir).name}"

    return _run


def run_named_set(
    name: str,
    n: int,
    *,
    clips: list[BenchClip] | None = None,
    transcribe: Callable[[BenchClip], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    spec = DATASETS[name]
    skip_reason: str | None = None
    loaded = clips
    if loaded is None:
        loaded, skip_reason = try_load_clips(spec, n)
    if skip_reason:
        return {
            "dataset": name,
            "hf_id": spec.hf_id,
            "licence": spec.licence,
            "proves": spec.proves,
            "not_claim": spec.not_claim,
            "skip_reason": skip_reason,
            "asr_status": asr_status(),
            "n": 0,
            "clips": [],
            "not_clinical_validation": True,
        }
    runner = transcribe
    reports: list[dict[str, Any]] = []
    for clip in loaded[:n]:
        clip_runner = runner
        if clip_runner is not None and (clip.audio_path is None or not clip.audio_path.is_file()):
            clip_runner = None
        try:
            reports.append(
                score_clip(clip, transcribe=clip_runner, char_level=spec.char_level)
            )
        except FileNotFoundError:
            reports.append(score_clip(clip, transcribe=None, char_level=spec.char_level))
    return summarise_reports(name, spec, reports)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a small public multilingual slice, or abstain honestly."
    )
    parser.add_argument(
        "--set",
        dest="dataset",
        choices=sorted(DATASETS),
        default="vimedcss",
    )
    parser.add_argument("--n", type=int, default=40)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    summary = run_named_set(args.dataset, args.n, transcribe=make_transcribe())
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{args.dataset}.json"
    out_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "dataset": summary["dataset"],
                "n": summary.get("n"),
                "asr_status": summary.get("asr_status"),
                "skip_reason": summary.get("skip_reason"),
                "path": str(out_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
