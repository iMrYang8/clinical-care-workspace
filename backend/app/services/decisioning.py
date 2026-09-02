from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    AIRun,
    CalibrationReport,
    ClinicalFactAssertion,
    ClinicMembership,
    DecisionAssessment,
    Entry,
    EntryVersion,
    EvaluationRun,
    Highlight,
    ProvenancePointer,
    RedactionEvaluationRun,
)
from app.services.clinical_formulary import allergy_category_for_assertion
from app.services.importance import is_safety_protected

RISK_RULE_VERSION = "clinical-risk-rules-v2"
REDACTOR_VERSION = "nightingale-redaction-v2"
RISK_ORDER = {"standard": 0, "elevated": 1, "high": 2, "critical": 3}
EVALUATION_MANIFEST_MISSING = "0" * 64
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$", re.I)
_GIT_REVISION = re.compile(r"^[0-9a-f]{7,64}$", re.I)

ReviewState = Literal["ready", "review_required", "abstained"]
PublicConfidenceState = Literal["qualified", "unavailable", "review_required"]


class DecisionPayload(TypedDict):
    review_state: ReviewState
    risk: dict[str, object]
    confidence: dict[str, object]
    importance: dict[str, object]
    abstention_reason: str | None


_CRITICAL = (
    ("ANAPHYLAXIS", re.compile(r"\b(?:anaphylaxis|anaphylactic)\b", re.I)),
    ("ACUTE_STROKE", re.compile(r"\b(?:stroke|facial droop|slurred speech)\b", re.I)),
    ("SEPSIS", re.compile(r"\bsepsis\b", re.I)),
    ("OVERDOSE", re.compile(r"\boverdose\b", re.I)),
    ("SUICIDALITY", re.compile(r"\b(?:suicidal|suicide)\b", re.I)),
)


@dataclass(frozen=True)
class RiskDecision:
    deterministic_floor: str
    model_risk: str | None
    effective_risk: str
    rule_ids: list[str]


@dataclass(frozen=True)
class ConfidenceQualification:
    qualified: bool
    current_state: Literal["qualified", "unavailable", "not_applicable"]
    band: str
    lower_bound: float | None
    reasons: tuple[str, ...] = ()


def public_confidence_projection(
    qualification: ConfidenceQualification,
) -> tuple[PublicConfidenceState, list[str]]:
    """Map internal qualification to an honest, stable public claim.

    Human-authored output has no model confidence to qualify. It remains
    non-blocking, but must not be presented as if a calibration report had
    qualified it.
    """

    if qualification.qualified and qualification.current_state == "qualified":
        return "qualified", []
    if qualification.current_state == "not_applicable":
        return "unavailable", ["CONFIDENCE_NOT_APPLICABLE"]
    return "review_required", list(qualification.reasons)


def risk_max(left: str, right: str | None) -> str:
    if right is None:
        return left
    return left if RISK_ORDER.get(left, 0) >= RISK_ORDER.get(right, 0) else right


def deterministic_risk(
    *, fact_type: str, text: str, conflict: bool = False, model_risk: str | None = None
) -> RiskDecision:
    floor = "standard"
    rules: list[str] = []
    lowered = fact_type.lower()
    for rule_id, pattern in _CRITICAL:
        if pattern.search(text):
            floor = "critical"
            rules.append(rule_id)
    if conflict and lowered == "allergy":
        floor = "critical"
        rules.append("ALLERGY_CONFLICT")
    elif conflict and lowered in {"medication", "dose", "route", "frequency"}:
        floor = risk_max(floor, "high")
        rules.append(f"{lowered.upper()}_CONFLICT")
    if lowered == "allergy" and re.search(r"\b(?:severe|anaphyl)\w*\b", text, re.I):
        floor = "critical"
        rules.append("SEVERE_ALLERGY")
    if not rules:
        rules.append("NO_DETERMINISTIC_FLOOR")
    return RiskDecision(
        deterministic_floor=floor,
        model_risk=model_risk,
        effective_risk=risk_max(floor, model_risk),
        rule_ids=sorted(set(rules)),
    )


def request_parameters_sha256(parameters: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def evaluation_manifest_sha256() -> str:
    manifest = (
        Path(__file__).resolve().parents[3]
        / "datasets"
        / "manifests"
        / "evaluation-pack-v1.json"
    )
    if not manifest.is_file():
        return EVALUATION_MANIFEST_MISSING
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def _valid_sha256(value: str) -> bool:
    return bool(_HEX_DIGEST.fullmatch(value)) and value != EVALUATION_MANIFEST_MISSING


def _valid_code_revision(value: str) -> bool:
    normalized = value.strip().casefold()
    return bool(_GIT_REVISION.fullmatch(normalized)) and normalized != "0" * len(
        normalized
    )


def _finite_metrics(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_metrics(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_finite_metrics(item) for item in value)
    return True


def qualify_calibration_report(
    session: Session,
    report: CalibrationReport | None,
    *,
    provider: str | None = None,
    exact_model_id: str | None = None,
    task: str | None = None,
    request_parameters: dict[str, object] | None = None,
    dataset_manifest_sha256: str | None = None,
    code_commit: str | None = None,
    now: datetime | None = None,
) -> ConfidenceQualification:
    """Revalidate calibration identity and statistical bounds at decision time."""

    if report is None:
        return ConfidenceQualification(
            qualified=False,
            current_state="unavailable",
            band="unavailable",
            lower_bound=None,
            reasons=("CALIBRATION_REPORT_MISSING",),
        )
    checked_at = now or datetime.now(UTC)
    expires_at = report.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    reasons: list[str] = []
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    if expires_at <= checked_at:
        reasons.append("CALIBRATION_REPORT_EXPIRED")
    if report.confidence_band not in {"high", "medium", "low"}:
        reasons.append("CALIBRATION_BAND_INVALID")
    lower_bound = report.accuracy_lower_bound
    if (
        lower_bound is None
        or not math.isfinite(lower_bound)
        or not 0.0 <= lower_bound <= 1.0
    ):
        reasons.append("CALIBRATION_LOWER_BOUND_INVALID")
    if report.holdout_sample_count < 100:
        reasons.append("CALIBRATION_SAMPLE_COUNT_INSUFFICIENT")
    if report.consultation_count < 10:
        reasons.append("CALIBRATION_CONSULTATION_COUNT_INSUFFICIENT")
    if report.consultation_count > report.holdout_sample_count:
        reasons.append("CALIBRATION_SAMPLE_COUNTS_INCONSISTENT")
    if (
        min(
            report.total_sample_count,
            report.calibration_sample_count,
            report.holdout_sample_count,
        )
        < 0
        or report.sample_count != report.holdout_sample_count
        or report.total_sample_count
        != report.calibration_sample_count + report.holdout_sample_count
    ):
        reasons.append("CALIBRATION_SAMPLE_COUNTS_INCONSISTENT")
    if not _valid_sha256(report.request_parameters_sha256):
        reasons.append("CALIBRATION_CONFIGURATION_IDENTITY_INVALID")
    if not _valid_sha256(report.dataset_manifest_sha256):
        reasons.append("CALIBRATION_DATASET_IDENTITY_INVALID")
    if not _valid_code_revision(report.code_commit):
        reasons.append("CALIBRATION_CODE_IDENTITY_INVALID")
    if code_commit is not None and not _valid_code_revision(code_commit):
        reasons.append("CALIBRATION_CODE_IDENTITY_INVALID")
    if not _finite_metrics(report.metrics_json):
        reasons.append("CALIBRATION_METRICS_NON_FINITE")
    expected_hash = (
        request_parameters_sha256(request_parameters)
        if request_parameters is not None
        else None
    )
    for mismatch, actual, expected in (
        ("CALIBRATION_PROVIDER_MISMATCH", report.provider, provider),
        ("CALIBRATION_MODEL_MISMATCH", report.exact_model_id, exact_model_id),
        ("CALIBRATION_TASK_MISMATCH", report.task, task),
        (
            "CALIBRATION_CONFIGURATION_MISMATCH",
            report.request_parameters_sha256,
            expected_hash,
        ),
        (
            "CALIBRATION_DATASET_MISMATCH",
            report.dataset_manifest_sha256,
            dataset_manifest_sha256,
        ),
        ("CALIBRATION_CODE_MISMATCH", report.code_commit, code_commit),
    ):
        if expected is not None and actual != expected:
            reasons.append(mismatch)

    run = session.exec(
        select(EvaluationRun).where(
            EvaluationRun.id == report.evaluation_run_id,
            EvaluationRun.clinic_id == report.clinic_id,
        )
    ).first()
    if run is None:
        reasons.append("CALIBRATION_EVALUATION_RUN_MISSING")
    else:
        if run.status != "completed":
            reasons.append("CALIBRATION_EVALUATION_RUN_INCOMPLETE")
        if not _valid_sha256(run.dataset_manifest_sha256):
            reasons.append("CALIBRATION_DATASET_IDENTITY_INVALID")
        if not _valid_code_revision(run.code_commit):
            reasons.append("CALIBRATION_CODE_IDENTITY_INVALID")
        if not _finite_metrics(run.metrics_json):
            reasons.append("CALIBRATION_METRICS_NON_FINITE")
        if (
            min(
                run.total_sample_count,
                run.calibration_sample_count,
                run.holdout_sample_count,
            )
            < 0
            or run.sample_count != run.holdout_sample_count
            or run.total_sample_count
            != run.calibration_sample_count + run.holdout_sample_count
        ):
            reasons.append("CALIBRATION_SAMPLE_COUNTS_INCONSISTENT")
        run_hash = request_parameters_sha256(run.request_parameters_json)
        for mismatch, actual, expected in (
            ("CALIBRATION_PROVIDER_MISMATCH", report.provider, run.provider),
            ("CALIBRATION_MODEL_MISMATCH", report.exact_model_id, run.exact_model_id),
            ("CALIBRATION_TASK_MISMATCH", report.task, run.task),
            (
                "CALIBRATION_CONFIGURATION_MISMATCH",
                report.request_parameters_sha256,
                run_hash,
            ),
            (
                "CALIBRATION_DATASET_MISMATCH",
                report.dataset_manifest_sha256,
                run.dataset_manifest_sha256,
            ),
            ("CALIBRATION_CODE_MISMATCH", report.code_commit, run.code_commit),
        ):
            if actual != expected:
                reasons.append(mismatch)
        if (
            report.holdout_sample_count != run.holdout_sample_count
            or report.calibration_sample_count != run.calibration_sample_count
            or report.total_sample_count != run.total_sample_count
        ):
            reasons.append("CALIBRATION_SAMPLE_COUNT_INCONSISTENT")

    unique_reasons = tuple(sorted(set(reasons)))
    if unique_reasons:
        return ConfidenceQualification(
            qualified=False,
            current_state="unavailable",
            band="unavailable",
            lower_bound=None,
            reasons=unique_reasons,
        )
    return ConfidenceQualification(
        qualified=True,
        current_state="qualified",
        band=report.confidence_band,
        lower_bound=lower_bound,
    )


def requalify_assessment_confidence(
    session: Session,
    assessment: DecisionAssessment | None,
    *,
    provider: str | None = None,
    exact_model_id: str | None = None,
    task: str | None = None,
    request_parameters: dict[str, object] | None = None,
    dataset_manifest_sha256: str | None = None,
    code_commit: str | None = None,
) -> ConfidenceQualification:
    if assessment is None or assessment.confidence_band == "not_applicable":
        return ConfidenceQualification(
            qualified=True,
            current_state="not_applicable",
            band="not_applicable",
            lower_bound=None,
        )
    if assessment.output_type == "extracted_fact":
        task = task or "clinical_fact_extraction"
        request_parameters = request_parameters or {
            "schema": "clinical-fact-v2",
            "prompt": "fact-extraction-v2",
        }
        dataset_manifest_sha256 = (
            dataset_manifest_sha256 or evaluation_manifest_sha256()
        )
        code_commit = code_commit or settings.NIGHTINGALE_SOURCE_COMMIT
        if assessment.assertion_id is not None:
            assertion = session.exec(
                select(ClinicalFactAssertion).where(
                    ClinicalFactAssertion.id == assessment.assertion_id,
                    ClinicalFactAssertion.clinic_id == assessment.clinic_id,
                )
            ).first()
            run = (
                session.exec(
                    select(AIRun)
                    .where(
                        AIRun.clinic_id == assessment.clinic_id,
                        AIRun.patient_id == assertion.patient_id,
                        AIRun.source_entry_version_id
                        == assertion.source_entry_version_id,
                    )
                    .order_by(col(AIRun.created_at).desc())
                ).first()
                if assertion is not None
                else None
            )
            if run is not None:
                provider = provider or run.provider
                exact_model_id = exact_model_id or run.model
    report = (
        session.exec(
            select(CalibrationReport).where(
                CalibrationReport.id == assessment.calibration_report_id,
                CalibrationReport.clinic_id == assessment.clinic_id,
            )
        ).first()
        if assessment.calibration_report_id is not None
        else None
    )
    qualification = qualify_calibration_report(
        session,
        report,
        provider=provider,
        exact_model_id=exact_model_id,
        task=task,
        request_parameters=request_parameters,
        dataset_manifest_sha256=dataset_manifest_sha256,
        code_commit=code_commit,
    )
    reasons = list(qualification.reasons)
    if report is not None:
        if assessment.calibration_version != str(report.id):
            reasons.append("ASSESSMENT_CALIBRATION_VERSION_MISMATCH")
        if assessment.confidence_band != report.confidence_band:
            reasons.append("ASSESSMENT_CONFIDENCE_BAND_MISMATCH")
        if (
            assessment.confidence_lower_bound is None
            or report.accuracy_lower_bound is None
            or not math.isclose(
                assessment.confidence_lower_bound,
                report.accuracy_lower_bound,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            reasons.append("ASSESSMENT_CONFIDENCE_BOUND_MISMATCH")
    if reasons:
        return ConfidenceQualification(
            qualified=False,
            current_state="unavailable",
            band="unavailable",
            lower_bound=None,
            reasons=tuple(sorted(set(reasons))),
        )
    return qualification


def matching_calibration_report(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    provider: str,
    exact_model_id: str,
    task: str,
    request_parameters: dict[str, object],
    dataset_manifest_sha256: str | None = None,
    code_commit: str | None = None,
) -> CalibrationReport | None:
    now = datetime.now(UTC)
    statement = select(CalibrationReport).where(
        CalibrationReport.clinic_id == clinic_id,
        CalibrationReport.provider == provider,
        CalibrationReport.exact_model_id == exact_model_id,
        CalibrationReport.task == task,
        CalibrationReport.request_parameters_sha256
        == request_parameters_sha256(request_parameters),
        CalibrationReport.expires_at > now,
        CalibrationReport.holdout_sample_count >= 100,
        CalibrationReport.consultation_count >= 10,
        col(CalibrationReport.confidence_band).in_(["high", "medium", "low"]),
    )
    if dataset_manifest_sha256:
        statement = statement.where(
            CalibrationReport.dataset_manifest_sha256 == dataset_manifest_sha256
        )
    if code_commit:
        statement = statement.where(CalibrationReport.code_commit == code_commit)
    candidates = session.exec(
        statement.order_by(col(CalibrationReport.created_at).desc())
    ).all()
    for candidate in candidates:
        if qualify_calibration_report(
            session,
            candidate,
            provider=provider,
            exact_model_id=exact_model_id,
            task=task,
            request_parameters=request_parameters,
            dataset_manifest_sha256=dataset_manifest_sha256,
            code_commit=code_commit,
            now=now,
        ).qualified:
            return candidate
    return None


def redaction_is_qualified(
    session: Session, *, clinic_id: uuid.UUID, dataset_sha256: str | None = None
) -> bool:
    statement = select(RedactionEvaluationRun).where(
        RedactionEvaluationRun.clinic_id == clinic_id,
        RedactionEvaluationRun.redactor_version == REDACTOR_VERSION,
        col(RedactionEvaluationRun.passed).is_(True),
        RedactionEvaluationRun.phi_recall >= 1.0,
        RedactionEvaluationRun.residual_phi_count == 0,
        RedactionEvaluationRun.clinical_span_damage_count == 0,
    )
    if dataset_sha256:
        statement = statement.where(
            RedactionEvaluationRun.dataset_sha256 == dataset_sha256
        )
    return (
        session.exec(
            statement.order_by(col(RedactionEvaluationRun.created_at).desc())
        ).first()
        is not None
    )


def create_assertion(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    patient_id: uuid.UUID,
    entry_id: uuid.UUID,
    source_entry_version_id: uuid.UUID,
    provenance_pointer: ProvenancePointer,
    fact_type: str,
    subject: str,
    normalized_value: str,
    origin: str,
    highlight_id: uuid.UUID | None = None,
    polarity: str = "present",
    assertion_scope: str = "specific_substance",
    source_language: str = "und",
    assertion_state: str = "active",
    clinical_status: str = "active",
    medication: str | None = None,
    dose_value: float | None = None,
    dose_unit: str | None = None,
    route: str | None = None,
    frequency: str | None = None,
) -> ClinicalFactAssertion:
    normalized_fact_type = fact_type.lower()
    allergy_category = (
        allergy_category_for_assertion(subject, assertion_scope)
        if normalized_fact_type == "allergy"
        else None
    )
    if normalized_fact_type in {"medication", "dose", "route", "frequency"}:
        medication = medication or subject
        if normalized_fact_type == "dose" and dose_value is None:
            dose_match = re.fullmatch(
                r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*", normalized_value
            )
            if dose_match is not None:
                dose_value = float(dose_match.group(1))
                dose_unit = dose_unit or dose_match.group(2).casefold()
        elif normalized_fact_type == "route" and route is None:
            route = normalized_value
        elif normalized_fact_type == "frequency" and frequency is None:
            frequency = normalized_value
    assertion_id = uuid.uuid4()
    source_entry = session.exec(
        select(Entry).where(
            Entry.clinic_id == clinic_id,
            Entry.id == entry_id,
        )
    ).first()
    source_version = session.exec(
        select(EntryVersion).where(
            EntryVersion.clinic_id == clinic_id,
            EntryVersion.id == source_entry_version_id,
        )
    ).first()
    source_section = source_entry.section if source_entry is not None else None
    source_role: str | None
    # The patient's own channel keeps patient attribution even when the
    # utterance was captured by the AI pipeline; ``origin`` already records how
    # it was captured, so collapsing the role to "system" would destroy the
    # clinically load-bearing answer to "who asserted this?".
    if source_section == "patient":
        source_role = "patient"
    elif origin in {"ai", "system"}:
        source_role = "system"
    elif source_version is not None:
        author_membership = session.exec(
            select(ClinicMembership).where(
                ClinicMembership.clinic_id == clinic_id,
                ClinicMembership.user_id == source_version.author_id,
            )
        ).first()
        source_role = author_membership.role if author_membership is not None else None
    else:
        source_role = None
    normalized_key = (
        f"{fact_type}:{assertion_scope}:{subject}:{normalized_value}:"
        f"{polarity}:{allergy_category or 'unavailable'}:{clinical_status}".lower()
    )
    assertion = ClinicalFactAssertion(
        id=assertion_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry_id=entry_id,
        source_entry_version_id=source_entry_version_id,
        provenance_pointer_id=provenance_pointer.id,
        highlight_id=highlight_id,
        fact_type=normalized_fact_type,
        subject_ciphertext=field_codec.encrypt_text(
            clinic_id, "fact_assertion.subject", assertion_id, subject
        ),
        normalized_value_ciphertext=field_codec.encrypt_text(
            clinic_id, "fact_assertion.normalized_value", assertion_id, normalized_value
        ),
        normalized_key_hash=hashlib.sha256(normalized_key.encode()).hexdigest(),
        polarity=polarity,
        assertion_scope=assertion_scope,
        allergy_category=allergy_category,
        source_language=source_language,
        source_role=source_role,
        source_section=source_section,
        assertion_state=assertion_state,
        clinical_status=clinical_status,
        medication_ciphertext=(
            field_codec.encrypt_text(
                clinic_id, "fact_assertion.medication", assertion_id, medication
            )
            if medication
            else None
        ),
        dose_value=dose_value,
        dose_unit=dose_unit,
        route=route,
        frequency=frequency,
        origin=origin,
    )
    session.add(assertion)
    session.flush()
    return assertion


def assessment_review_state(
    assessment: DecisionAssessment | None,
    highlight: Highlight,
    confidence_qualification: ConfidenceQualification | None = None,
) -> ReviewState:
    if highlight.unresolved:
        return "review_required"
    if confidence_qualification is not None and not confidence_qualification.qualified:
        return "review_required"
    if assessment is None:
        return "review_required" if highlight.review_required else "ready"
    if assessment.abstained:
        return "abstained"
    if (
        assessment.support_state
        not in {
            "supported",
            "human_asserted",
            "human_confirmed",
        }
        or highlight.review_required
    ):
        return "review_required"
    return "ready"


def decision_payload(
    *,
    assessment: DecisionAssessment | None,
    highlight: Highlight,
    score_components: dict[str, float],
    confidence_qualification: ConfidenceQualification | None = None,
    importance_mode: Literal["disabled", "shadow", "active"] = "shadow",
) -> DecisionPayload:
    state = assessment_review_state(assessment, highlight, confidence_qualification)
    risk: dict[str, object] = {
        "effective": assessment.effective_risk
        if assessment
        else ("critical" if highlight.critical else "standard"),
        "floor": assessment.deterministic_floor
        if assessment
        else ("critical" if highlight.critical else "standard"),
        "model": assessment.model_risk if assessment else None,
        "rule_ids": assessment.risk_rule_ids_json
        if assessment
        else (["MANUAL_CRITICAL"] if highlight.critical else []),
        "rule_version": assessment.risk_rule_version
        if assessment
        else RISK_RULE_VERSION,
    }
    effective_band = (
        confidence_qualification.band
        if confidence_qualification is not None
        else (assessment.confidence_band if assessment else "not_applicable")
    )
    effective_lower_bound = (
        confidence_qualification.lower_bound
        if confidence_qualification is not None
        else (assessment.confidence_lower_bound if assessment else None)
    )
    confidence: dict[str, object] = {
        "band": effective_band,
        "lower_bound": effective_lower_bound,
        "calibration_report_id": str(assessment.calibration_report_id)
        if assessment and assessment.calibration_report_id
        else None,
        "current_state": (
            confidence_qualification.current_state
            if confidence_qualification is not None
            else "unverified"
        ),
        "reasons": (
            list(confidence_qualification.reasons)
            if confidence_qualification is not None
            else []
        ),
    }
    importance: dict[str, object] = {
        "score": highlight.final_score,
        "components": score_components,
        "protected": is_safety_protected(highlight),
        "mode": importance_mode,
    }
    return {
        "review_state": state,
        "risk": risk,
        "confidence": confidence,
        "importance": importance,
        "abstention_reason": assessment.abstention_reason if assessment else None,
    }
