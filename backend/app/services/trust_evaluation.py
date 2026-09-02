from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import random
import subprocess
import tempfile
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.models import (
    CalibrationBucket,
    CalibrationReport,
    EvaluationRun,
    RedactionEvaluationRun,
    get_datetime_utc,
)
from app.services.conflicts import extract_normalized_facts
from app.services.dataset_imports import (
    merge_reference_segments,
    parse_textgrid,
    stereo_wav,
)
from app.services.decisioning import (
    REDACTOR_VERSION,
    request_parameters_sha256,
)
from app.services.egress import TextModelEgressGateway
from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
    validate_evidence,
)
from app.services.providers.openai_text import OpenAITextProvider
from app.services.redaction import RedactionService
from app.services.voice.providers.openai_audio import OpenAIAudioTranscriptionProvider


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split()
        if token
    ]


def _edit_distance(left: list[str], right: list[str]) -> int:
    prior = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, prior[j] + 1, prior[j - 1] + (a != b)))
        prior = current
    return prior[-1]


def _error_rate(reference: str, prediction: str, *, characters: bool = False) -> float:
    left = list(reference.lower()) if characters else _tokens(reference)
    right = list(prediction.lower()) if characters else _tokens(prediction)
    return _edit_distance(left, right) / max(1, len(left))


def _wilson_lower(successes: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


def _band(lower: float | None, samples: int, consultations: int) -> str:
    if lower is None or samples < 100 or consultations < 10:
        return "unavailable"
    if lower >= 0.95:
        return "high"
    if lower >= 0.85:
        return "medium"
    return "low"


def _metrics(
    outcomes: list[bool], predicted_probability: float
) -> dict[str, float | int]:
    if not outcomes:
        return {"sample_count": 0, "accuracy": 0.0, "ece": 1.0, "brier": 1.0}
    accuracy = sum(outcomes) / len(outcomes)
    brier = sum((predicted_probability - int(value)) ** 2 for value in outcomes) / len(
        outcomes
    )
    return {
        "sample_count": len(outcomes),
        "accuracy": round(accuracy, 6),
        "ece": round(abs(accuracy - predicted_probability), 6),
        "brier": round(brier, 6),
        "selective_accuracy": round(accuracy, 6),
        "coverage": 1.0,
    }


def _cluster_bootstrap_interval(
    clusters: list[list[bool]], *, samples: int = 2_000
) -> list[float] | None:
    """Deterministic consultation-level bootstrap interval for accuracy."""

    if not clusters:
        return None
    rng = random.Random(20260827)
    estimates: list[float] = []
    for _ in range(samples):
        draw = [clusters[rng.randrange(len(clusters))] for _ in clusters]
        values = [value for cluster in draw for value in cluster]
        estimates.append(sum(values) / max(1, len(values)))
    estimates.sort()
    return [
        round(estimates[int(0.025 * (samples - 1))], 6),
        round(estimates[int(0.975 * (samples - 1))], 6),
    ]


def _split(ids: list[str], calibration_count: int) -> tuple[list[str], list[str]]:
    ordered = sorted(ids, key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    return ordered[:calibration_count], ordered[calibration_count:]


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _persist_report(
    session: Session,
    *,
    context: RequestContext,
    provider: str,
    model: str,
    task: str,
    request_parameters: dict[str, object],
    manifest_sha256: str,
    code_commit: str,
    calibration_ids: list[str],
    holdout_ids: list[str],
    calibration_outcomes: list[bool],
    holdout_outcomes: list[bool],
    metrics: dict[str, object],
) -> CalibrationReport:
    now = get_datetime_utc()
    calibration_sample_count = len(calibration_outcomes)
    holdout_sample_count = len(holdout_outcomes)
    total_sample_count = calibration_sample_count + holdout_sample_count
    estimated = sum(calibration_outcomes) / max(1, calibration_sample_count)
    lower = _wilson_lower(sum(holdout_outcomes), holdout_sample_count)
    band = _band(lower, holdout_sample_count, len(holdout_ids))
    request_hash = request_parameters_sha256(request_parameters)
    calibration_split = hashlib.sha256("\n".join(calibration_ids).encode()).hexdigest()
    holdout_split = hashlib.sha256("\n".join(holdout_ids).encode()).hexdigest()
    existing = session.exec(
        select(CalibrationReport)
        .join(
            EvaluationRun,
            (col(EvaluationRun.clinic_id) == col(CalibrationReport.clinic_id))
            & (col(EvaluationRun.id) == col(CalibrationReport.evaluation_run_id)),
        )
        .where(
            CalibrationReport.clinic_id == context.clinic_id,
            CalibrationReport.provider == provider,
            CalibrationReport.exact_model_id == model,
            CalibrationReport.task == task,
            CalibrationReport.request_parameters_sha256 == request_hash,
            CalibrationReport.dataset_manifest_sha256 == manifest_sha256,
            CalibrationReport.code_commit == code_commit,
            EvaluationRun.calibration_split == calibration_split,
            EvaluationRun.holdout_split == holdout_split,
            EvaluationRun.total_sample_count == total_sample_count,
            EvaluationRun.calibration_sample_count == calibration_sample_count,
            EvaluationRun.holdout_sample_count == holdout_sample_count,
            CalibrationReport.total_sample_count == total_sample_count,
            CalibrationReport.calibration_sample_count == calibration_sample_count,
            CalibrationReport.holdout_sample_count == holdout_sample_count,
            EvaluationRun.status == "completed",
            CalibrationReport.expires_at > now,
        )
        .order_by(col(CalibrationReport.created_at).desc())
    ).first()
    if existing is not None:
        return existing
    run = EvaluationRun(
        clinic_id=context.clinic_id,
        provider=provider,
        exact_model_id=model,
        task=task,
        request_parameters_json=request_parameters,
        dataset_manifest_sha256=manifest_sha256,
        code_commit=code_commit,
        calibration_split=calibration_split,
        holdout_split=holdout_split,
        total_sample_count=total_sample_count,
        calibration_sample_count=calibration_sample_count,
        holdout_sample_count=holdout_sample_count,
        # ``sample_count`` remains a compatibility projection of the untouched
        # holdout. The explicit fields prevent total/calibration accounting from
        # being inferred from metrics JSON at decision time.
        sample_count=holdout_sample_count,
        status="completed",
        metrics_json={
            **metrics,
            "calibration_sample_count": calibration_sample_count,
            "holdout_sample_count": holdout_sample_count,
            "total_sample_count": total_sample_count,
        },
    )
    session.add(run)
    session.flush()
    report = CalibrationReport(
        clinic_id=context.clinic_id,
        evaluation_run_id=run.id,
        provider=provider,
        exact_model_id=model,
        task=task,
        request_parameters_sha256=request_hash,
        dataset_manifest_sha256=manifest_sha256,
        code_commit=code_commit,
        total_sample_count=total_sample_count,
        calibration_sample_count=calibration_sample_count,
        holdout_sample_count=holdout_sample_count,
        sample_count=holdout_sample_count,
        consultation_count=len(holdout_ids),
        confidence_band=band,
        accuracy_lower_bound=lower,
        metrics_json={"estimated_accuracy": estimated, **metrics},
        expires_at=now + timedelta(days=30),
    )
    session.add(report)
    session.flush()
    session.add(
        CalibrationBucket(
            clinic_id=context.clinic_id,
            calibration_report_id=report.id,
            bucket_key="overall",
            sample_count=holdout_sample_count,
            consultation_count=len(holdout_ids),
            estimated_accuracy=estimated,
            accuracy_lower_bound=lower,
            confidence_band=band,
            metrics_json=metrics,
        )
    )
    session.commit()
    session.refresh(report)
    return report


def run_redaction_evaluation(
    session: Session,
    *,
    context: RequestContext,
    output_dir: Path,
) -> RedactionEvaluationRun:
    templates = [
        (
            "Alice Tan",
            "S1234567D",
            "MRN-2026-00001",
            "+65 9123 4567",
            "alice@example.com",
        ),
        ("陈小明", "T7654321A", "MRN:ZXCVB123", "81234567", "ming.chen@example.sg"),
        (
            "Mary Ann Lee",
            "F1234567N",
            "MRN ABCDE678",
            "61234567",
            "mary.lee@example.org",
        ),
        ("Nur Aisyah", "G7654321X", "MRN-QWERT987", "98765432", "aisyah@example.net"),
        ("José Lim", "M1234567K", "MRN 1ABCD234", "31234567", "jose.lim@example.com"),
    ]
    clinical = "penicillin allergy; metformin 500 mg PO BID; severe rash"
    service = RedactionService(require_presidio=False)
    false_negative = 0
    clinical_damage = 0
    detected = 0
    expected = 0
    per_class = {
        name: {"true_positive": 0, "false_positive": 0, "false_negative": 0}
        for name in ("name", "nric_fin", "mrn", "phone", "email")
    }
    entity_types = {
        "name": "KNOWN_NAME",
        "nric_fin": "NRIC_FIN",
        "mrn": "MRN",
        "phone": "SG_PHONE",
        "email": "EMAIL",
    }
    for index in range(500):
        name, identity, mrn, phone, email = templates[index % len(templates)]
        text = f"Patient {name}; ID {identity}; {mrn}; phone {phone}; email {email}; {clinical}."
        record_id = uuid.uuid5(uuid.NAMESPACE_URL, f"redaction-gold-v2:{index}")
        result = service.redact(
            text,
            clinic_id=context.clinic_id,
            record_id=record_id,
            known_names=[name],
        )
        for label, value in zip(
            ("name", "nric_fin", "mrn", "phone", "email"),
            (name, identity, mrn, phone, email),
            strict=True,
        ):
            expected += 1
            if value.lower() in result.redacted_text.lower():
                false_negative += 1
                per_class[label]["false_negative"] += 1
            else:
                detected += 1
                per_class[label]["true_positive"] += 1
            predicted = result.entity_counts.get(entity_types[label], 0)
            per_class[label]["false_positive"] += max(
                0, predicted - per_class[label]["true_positive"]
            )
        if clinical not in result.redacted_text:
            clinical_damage += 1
    dataset_sha = hashlib.sha256(
        json.dumps(
            {"templates": templates, "clinical": clinical},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    recall = detected / expected
    class_metrics: dict[str, dict[str, float | int]] = {}
    for label, counts in per_class.items():
        tp = counts["true_positive"]
        fp = counts["false_positive"]
        fn = counts["false_negative"]
        class_metrics[label] = {
            **counts,
            "precision": tp / max(1, tp + fp),
            "recall": tp / max(1, tp + fn),
        }
    row = RedactionEvaluationRun(
        clinic_id=context.clinic_id,
        redactor_version=REDACTOR_VERSION,
        dataset_sha256=dataset_sha,
        sample_count=500,
        phi_recall=recall,
        residual_phi_count=false_negative,
        clinical_span_damage_count=clinical_damage,
        passed=recall == 1.0 and false_negative == 0 and clinical_damage == 0,
        metrics_json={
            "expected_phi_spans": expected,
            "detected_phi_spans": detected,
            "false_negatives": false_negative,
            "clinical_span_damage": clinical_damage,
            "per_class": class_metrics,
        },
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    _write_json(
        output_dir / "redaction-v2.json",
        {
            "redactor_version": row.redactor_version,
            "dataset_sha256": dataset_sha,
            "sample_count": row.sample_count,
            "phi_recall": row.phi_recall,
            "residual_phi_count": row.residual_phi_count,
            "clinical_span_damage_count": row.clinical_span_damage_count,
            "passed": row.passed,
            "metrics": row.metrics_json,
        },
    )
    return row


@dataclass(frozen=True)
class VoiceCase:
    source_id: str
    outcomes: list[bool]
    wer: float
    cer: float
    medical_entity_recall: float
    speaker_accuracy: float
    timestamp_error_ms: float
    provider_error: bool


def _temporal_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


async def _voice_case(
    *,
    source_id: str,
    repository: Path,
    audio_root: Path,
    provider: OpenAIAudioTranscriptionProvider,
    cache_dir: Path,
) -> VoiceCase:
    cache_path = cache_dir / f"{source_id}.json"
    doctor_ref = parse_textgrid(
        repository / "transcripts" / f"{source_id}_doctor.TextGrid", "doctor"
    )
    patient_ref = parse_textgrid(
        repository / "transcripts" / f"{source_id}_patient.TextGrid", "patient"
    )
    references = merge_reference_segments(doctor_ref, patient_ref)
    raw: dict[str, Any]
    if cache_path.exists():
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        raw = (
            loaded
            if isinstance(loaded, dict)
            else {"_evaluation_error": "INVALID_CACHE_PAYLOAD"}
        )
    else:
        audio, _ = stereo_wav(
            audio_root / f"{source_id}_doctor.wav",
            audio_root / f"{source_id}_patient.wav",
        )
        with (
            tempfile.NamedTemporaryFile(suffix=".wav") as wav_target,
            tempfile.NamedTemporaryFile(suffix=".flac") as flac_target,
        ):
            wav_target.write(audio)
            wav_target.flush()
            # Preserve both diarization channels while staying below provider
            # upload limits. FLAC is lossless, so reference WER and timestamp
            # measurements are not confounded by lossy evaluation encoding.
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    wav_target.name,
                    "-compression_level",
                    "8",
                    flac_target.name,
                ],
                check=True,
                timeout=180,
            )
            result = None
            error_code: str | None = None
            for attempt in range(3):
                try:
                    result = await provider.transcribe(Path(flac_target.name))
                    break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    error_code = f"PROVIDER_HTTP_{status}"
                    if status not in {
                        408,
                        409,
                        429,
                        500,
                        502,
                        503,
                        504,
                    }:
                        break
                except (httpx.TimeoutException, httpx.TransportError):
                    error_code = "PROVIDER_TRANSIENT_TRANSPORT_ERROR"
                except ValueError:
                    error_code = "PROVIDER_INVALID_TRANSCRIPT_RESPONSE"
                    break
                if attempt == 2:
                    break
                await asyncio.sleep(5 * (2**attempt))
        raw = (
            {
                "text": result.text,
                "segments": [asdict(item) for item in result.segments],
                "warnings": list(result.warnings),
            }
            if result is not None
            else {
                "text": "",
                "segments": [],
                "_evaluation_error": error_code or "PROVIDER_REQUEST_FAILED",
            }
        )
        _write_json(cache_path, raw)
    raw_segments = raw.get("segments")
    predicted_segments: list[dict[str, Any]] = (
        [item for item in raw_segments if isinstance(item, dict)]
        if isinstance(raw_segments, list)
        else []
    )
    label_votes: dict[str, Counter[str]] = {}
    matched: list[tuple[Any, dict[str, Any] | None]] = []
    for reference in references:
        candidate = max(
            predicted_segments,
            key=lambda item: _temporal_overlap(
                reference.start_ms,
                reference.end_ms,
                int(item["start_ms"]),
                int(item["end_ms"]),
            ),
            default=None,
        )
        if (
            candidate is not None
            and _temporal_overlap(
                reference.start_ms,
                reference.end_ms,
                int(candidate["start_ms"]),
                int(candidate["end_ms"]),
            )
            > 0
        ):
            label = str(candidate.get("speaker_id") or "unknown")
            label_votes.setdefault(label, Counter())[reference.speaker] += 1
            matched.append((reference, candidate))
        else:
            matched.append((reference, None))
    mapping = {
        label: votes.most_common(1)[0][0]
        for label, votes in label_votes.items()
        if votes
    }
    outcomes: list[bool] = []
    timestamp_errors: list[float] = []
    speaker_correct = 0
    reference_text = " ".join(item.text for item in references)
    prediction_text = str(raw.get("text", ""))
    reference_entities = {
        (item.fact_type, item.key, item.value)
        for item in extract_normalized_facts(reference_text)
    }
    predicted_entities = {
        (item.fact_type, item.key, item.value)
        for item in extract_normalized_facts(prediction_text)
    }
    entity_recall = len(reference_entities & predicted_entities) / max(
        1, len(reference_entities)
    )
    for reference, candidate in matched:
        if candidate is None:
            outcomes.append(False)
            continue
        segment_wer = _error_rate(reference.text, str(candidate.get("text", "")))
        speaker_ok = (
            mapping.get(str(candidate.get("speaker_id") or "unknown"))
            == reference.speaker
        )
        speaker_correct += int(speaker_ok)
        time_error = (
            abs(reference.start_ms - int(candidate["start_ms"]))
            + abs(reference.end_ms - int(candidate["end_ms"]))
        ) / 2
        timestamp_errors.append(time_error)
        outcomes.append(segment_wer <= 0.10 and speaker_ok and time_error <= 2_000)
    return VoiceCase(
        source_id=source_id,
        outcomes=outcomes,
        wer=_error_rate(reference_text, prediction_text),
        cer=_error_rate(reference_text, prediction_text, characters=True),
        medical_entity_recall=entity_recall,
        speaker_accuracy=speaker_correct / max(1, len(matched)),
        timestamp_error_ms=sum(timestamp_errors) / max(1, len(timestamp_errors)),
        provider_error=bool(raw.get("_evaluation_error")),
    )


async def run_voice_evaluation(
    session: Session,
    *,
    context: RequestContext,
    raw_root: Path,
    manifest_path: Path,
    output_dir: Path,
    api_key: str,
    model: str,
    code_commit: str,
) -> CalibrationReport:
    repository = next((raw_root / "primock57" / "extracted").glob("primock57-*"))
    audio_root = raw_root / "primock57" / "audio"
    source_ids = sorted(
        path.name.removesuffix("_doctor.wav")
        for path in audio_root.glob("*_doctor.wav")
        if (audio_root / path.name.replace("_doctor.wav", "_patient.wav")).is_file()
    )
    if not source_ids:
        raise RuntimeError(
            "No hydrated PriMock57 doctor/patient WAV pairs are available under "
            f"{audio_root}"
        )
    # The canonical 57-consultation run is 40 calibration / 17 holdout.  A
    # partial local pack is still measurable but can never qualify confidence;
    # keep every available consultation in holdout so the negative result is
    # visible instead of producing an empty report.
    calibration_count = 40 if len(source_ids) >= 57 else max(0, len(source_ids) - 17)
    calibration_ids, holdout_ids = _split(source_ids, calibration_count)
    provider = OpenAIAudioTranscriptionProvider(api_key=api_key, model=model)
    semaphore = asyncio.Semaphore(3)

    async def evaluate_one(source_id: str) -> tuple[str, VoiceCase]:
        async with semaphore:
            return source_id, await _voice_case(
                source_id=source_id,
                repository=repository,
                audio_root=audio_root,
                provider=provider,
                cache_dir=output_dir / "cache" / "voice",
            )

    cases = dict(await asyncio.gather(*(evaluate_one(item) for item in source_ids)))
    calibration_outcomes = [
        value for item in calibration_ids for value in cases[item].outcomes
    ]
    holdout_outcomes = [value for item in holdout_ids for value in cases[item].outcomes]
    estimated = sum(calibration_outcomes) / max(1, len(calibration_outcomes))
    scored_cases = [item for item in cases.values() if not item.provider_error]
    aggregate: dict[str, object] = {
        "wer": sum(item.wer for item in cases.values()) / len(cases),
        "cer": sum(item.cer for item in cases.values()) / len(cases),
        "medical_entity_recall": sum(
            item.medical_entity_recall for item in cases.values()
        )
        / len(cases),
        "speaker_error_rate": 1
        - sum(item.speaker_accuracy for item in cases.values()) / len(cases),
        "timestamp_error_ms": sum(item.timestamp_error_ms for item in scored_cases)
        / max(1, len(scored_cases)),
        "provider_error_count": sum(item.provider_error for item in cases.values()),
        **_metrics(holdout_outcomes, estimated),
        "precision_by_bucket": {
            "overall": sum(holdout_outcomes) / max(1, len(holdout_outcomes))
        },
        "selective_accuracy_coverage": [
            {
                "coverage": 1.0,
                "accuracy": sum(holdout_outcomes) / max(1, len(holdout_outcomes)),
            }
        ],
        "cluster_bootstrap_95_ci": _cluster_bootstrap_interval(
            [cases[item].outcomes for item in holdout_ids]
        ),
    }
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    report = _persist_report(
        session,
        context=context,
        provider="openai",
        model=model,
        task="voice_transcription",
        request_parameters={
            "response_format": "diarized_json",
            "chunking_strategy": "auto",
        },
        manifest_sha256=manifest_sha,
        code_commit=code_commit,
        calibration_ids=calibration_ids,
        holdout_ids=holdout_ids,
        calibration_outcomes=calibration_outcomes,
        holdout_outcomes=holdout_outcomes,
        metrics=aggregate,
    )
    _write_json(
        output_dir / "voice-calibration.json",
        {
            "provider": report.provider,
            "exact_model_id": report.exact_model_id,
            "task": report.task,
            "dataset_manifest_sha256": report.dataset_manifest_sha256,
            "sample_count": report.sample_count,
            "total_sample_count": report.total_sample_count,
            "calibration_sample_count": report.calibration_sample_count,
            "holdout_sample_count": report.holdout_sample_count,
            "consultation_count": report.consultation_count,
            "confidence_band": report.confidence_band,
            "accuracy_lower_bound": report.accuracy_lower_bound,
            "metrics": report.metrics_json,
            "negative_results_are_preserved": True,
        },
    )
    return report


async def _fact_case(
    provider: OpenAITextProvider,
    row: dict[str, str],
    cache_dir: Path,
    *,
    redaction_service: RedactionService | None = None,
) -> tuple[list[bool], dict[str, int]]:
    dialogue = row["dialogue"]
    evaluation_clinic_id = uuid.uuid5(
        uuid.NAMESPACE_URL, "nightingale:trust-evaluation"
    )
    source_version_id = uuid.uuid5(
        evaluation_clinic_id, f"fact-case:{row['encounter_id']}"
    )
    known_names = [
        row[key].strip()
        for key in ("patient_name", "patient")
        if row.get(key, "").strip()
    ]
    report = (redaction_service or RedactionService(require_presidio=True)).redact(
        dialogue,
        clinic_id=evaluation_clinic_id,
        record_id=source_version_id,
        known_names=known_names,
    )
    context = ExtractionContext(
        clinic_id=evaluation_clinic_id,
        patient_id=uuid.uuid5(evaluation_clinic_id, row["encounter_id"]),
        source_version_id=source_version_id,
        interaction_type="doctor_consult",
    )
    cache_key = hashlib.sha256(
        (
            f"qualified-redacted-draft-v1\0{provider.extract_model}\0"
            f"{row['encounter_id']}\0{report.redacted_sha256}"
        ).encode()
    ).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    draft: ClinicalNoteDraft | None = None
    error_code: str | None = None
    if cache_path.exists():
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            isinstance(loaded, dict)
            and loaded.get("schema") == "qualified-redacted-draft-v1"
            and loaded.get("redacted_sha256") == report.redacted_sha256
        ):
            if isinstance(loaded.get("error_code"), str):
                error_code = str(loaded["error_code"])
            else:
                raw_draft = loaded.get("draft")
                if isinstance(raw_draft, dict):
                    raw_facts = raw_draft.get("facts", [])
                    if isinstance(raw_facts, list):
                        try:
                            draft = ClinicalNoteDraft(
                                summary=str(raw_draft.get("summary", "")),
                                facts=[
                                    ClinicalFact(
                                        fact_type=str(item["fact_type"]),
                                        value=str(item["value"]),
                                        evidence_start=int(item["evidence_start"]),
                                        evidence_end=int(item["evidence_end"]),
                                        evidence_quote=str(item["evidence_quote"]),
                                        feature_keys=[
                                            str(value)
                                            for value in item.get("feature_keys", [])
                                        ],
                                        critical=bool(item.get("critical", False)),
                                    )
                                    for item in raw_facts
                                    if isinstance(item, dict)
                                ],
                                provider=str(raw_draft.get("provider", "openai")),
                                model=str(
                                    raw_draft.get("model", provider.extract_model)
                                ),
                                warnings=[
                                    str(item) for item in raw_draft.get("warnings", [])
                                ],
                                needs_review=bool(raw_draft.get("needs_review", False)),
                            )
                        except (KeyError, TypeError, ValueError):
                            draft = None
    if draft is None and error_code is None:
        error_code = "PROVIDER_REQUEST_FAILED"
        for attempt in range(5):
            try:
                draft = validate_evidence(
                    await TextModelEgressGateway(provider).extract(report, context),
                    report.redacted_text,
                )
                error_code = None
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                error_code = f"PROVIDER_HTTP_{status}"
                if status not in {408, 409, 429, 500, 502, 503, 504}:
                    break
            except (httpx.TimeoutException, httpx.TransportError):
                error_code = "PROVIDER_TRANSIENT_TRANSPORT_ERROR"
            except ValueError:
                error_code = "REDACTION_EGRESS_NOT_QUALIFIED"
                break
            if attempt < 4:
                await asyncio.sleep(2**attempt)
        cache_payload: dict[str, object] = {
            "schema": "qualified-redacted-draft-v1",
            "redacted_sha256": report.redacted_sha256,
        }
        if draft is None:
            # Keep only a fixed taxonomy code. Provider error bodies can echo
            # submitted text and therefore never belong in logs or reports.
            cache_payload["error_code"] = error_code
        else:
            cache_payload["draft"] = asdict(draft)
        _write_json(cache_path, cache_payload)
    if draft is None:
        return [False], {
            "true_positive": 0,
            "false_positive": 0,
            "false_negative": 1,
            "exact_evidence_fact_count": 0,
            "provider_error": 1,
        }
    reference = {
        (item.fact_type, item.key, item.value)
        for item in extract_normalized_facts(row["note"])
    }
    predicted: set[tuple[str, str, str]] = set()
    for fact in draft.facts:
        normalized = extract_normalized_facts(fact.evidence_quote)
        predicted.update((item.fact_type, item.key, item.value) for item in normalized)
    outcomes = [item in reference for item in predicted]
    outcomes.extend(False for _ in reference - predicted)
    counts = {
        "true_positive": len(reference & predicted),
        "false_positive": len(predicted - reference),
        "false_negative": len(reference - predicted),
        "exact_evidence_fact_count": len(predicted),
    }
    return outcomes, counts


async def run_fact_evaluation(
    session: Session,
    *,
    context: RequestContext,
    raw_root: Path,
    manifest_path: Path,
    output_dir: Path,
    api_key: str,
    model: str,
    code_commit: str,
) -> CalibrationReport:
    root = raw_root / "aci_bench" / "extracted" / "aci-bench-corpus" / "challenge_data"
    calibration_path = root / "valid.csv"
    holdout_path = root / "clinicalnlp_taskB_test1.csv"

    def rows(path: Path, limit: int) -> list[dict[str, str]]:
        with path.open(encoding="utf-8") as source:
            return list(csv.DictReader(source))[:limit]

    calibration_rows = rows(calibration_path, 100)
    holdout_rows = rows(holdout_path, 100)
    provider = OpenAITextProvider(
        api_key=api_key,
        extract_model=model,
        timeout_seconds=120,
    )
    cache_dir = output_dir / "cache" / "facts"
    outcomes: dict[str, list[bool]] = {"calibration": [], "holdout": []}
    case_outcomes: dict[str, list[list[bool]]] = {
        "calibration": [],
        "holdout": [],
    }
    totals: Counter[str] = Counter()

    async def evaluate_split(
        items: list[dict[str, str]],
    ) -> list[tuple[list[bool], dict[str, int]]]:
        # Bound concurrency to avoid provider bursts while making the 200-case
        # benchmark practical. Each completed raw response is cached before it
        # is scored, so an interrupted run resumes without replaying calls.
        semaphore = asyncio.Semaphore(5)

        async def evaluate_one(
            row: dict[str, str],
        ) -> tuple[list[bool], dict[str, int]]:
            async with semaphore:
                return await _fact_case(provider, row, cache_dir)

        return await asyncio.gather(*(evaluate_one(row) for row in items))

    for split, items in (("calibration", calibration_rows), ("holdout", holdout_rows)):
        for result, counts in await evaluate_split(items):
            outcomes[split].extend(result)
            case_outcomes[split].append(result)
            totals.update(counts)
    estimated = sum(outcomes["calibration"]) / max(1, len(outcomes["calibration"]))
    aggregate: dict[str, object] = {
        **totals,
        **_metrics(outcomes["holdout"], estimated),
        "supported_fact_types": ["allergy", "medication", "dose", "route", "frequency"],
        "evidence_source": "original_dialogue",
        "precision_by_bucket": {
            "overall": sum(outcomes["holdout"]) / max(1, len(outcomes["holdout"]))
        },
        "selective_accuracy_coverage": [
            {
                "coverage": 1.0,
                "accuracy": sum(outcomes["holdout"]) / max(1, len(outcomes["holdout"])),
            }
        ],
        "cluster_bootstrap_95_ci": _cluster_bootstrap_interval(
            case_outcomes["holdout"]
        ),
    }
    report = _persist_report(
        session,
        context=context,
        provider="openai",
        model=model,
        task="clinical_fact_extraction",
        request_parameters={
            "schema": "clinical-fact-v2",
            "prompt": "fact-extraction-v2",
        },
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        code_commit=code_commit,
        calibration_ids=[row["encounter_id"] for row in calibration_rows],
        holdout_ids=[row["encounter_id"] for row in holdout_rows],
        calibration_outcomes=outcomes["calibration"],
        holdout_outcomes=outcomes["holdout"],
        metrics=aggregate,
    )
    _write_json(
        output_dir / "fact-calibration.json",
        {
            "provider": report.provider,
            "exact_model_id": report.exact_model_id,
            "task": report.task,
            "dataset_manifest_sha256": report.dataset_manifest_sha256,
            "sample_count": report.sample_count,
            "total_sample_count": report.total_sample_count,
            "calibration_sample_count": report.calibration_sample_count,
            "holdout_sample_count": report.holdout_sample_count,
            "consultation_count": report.consultation_count,
            "confidence_band": report.confidence_band,
            "accuracy_lower_bound": report.accuracy_lower_bound,
            "metrics": report.metrics_json,
            "negative_results_are_preserved": True,
        },
    )
    return report
