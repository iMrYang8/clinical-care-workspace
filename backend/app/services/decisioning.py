from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, col, select

from app.core.field_crypto import field_codec
from app.models import (
    CalibrationReport,
    ClinicalFactAssertion,
    DecisionAssessment,
    Highlight,
    ProvenancePointer,
    RedactionEvaluationRun,
)

RISK_RULE_VERSION = "clinical-risk-rules-v2"
REDACTOR_VERSION = "nightingale-redaction-v2"
RISK_ORDER = {"standard": 0, "elevated": 1, "high": 2, "critical": 3}
EVALUATION_MANIFEST_MISSING = "0" * 64

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
        CalibrationReport.sample_count >= 100,
        CalibrationReport.consultation_count >= 10,
        col(CalibrationReport.confidence_band).in_(["high", "medium", "low"]),
    )
    if dataset_manifest_sha256:
        statement = statement.where(
            CalibrationReport.dataset_manifest_sha256 == dataset_manifest_sha256
        )
    if code_commit:
        statement = statement.where(CalibrationReport.code_commit == code_commit)
    return session.exec(
        statement.order_by(col(CalibrationReport.created_at).desc())
    ).first()


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
    clinical_status: str = "active",
    medication: str | None = None,
    dose_value: float | None = None,
    dose_unit: str | None = None,
    route: str | None = None,
    frequency: str | None = None,
) -> ClinicalFactAssertion:
    assertion_id = uuid.uuid4()
    normalized_key = (
        f"{fact_type}:{subject}:{normalized_value}:{polarity}:{clinical_status}".lower()
    )
    assertion = ClinicalFactAssertion(
        id=assertion_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        entry_id=entry_id,
        source_entry_version_id=source_entry_version_id,
        provenance_pointer_id=provenance_pointer.id,
        highlight_id=highlight_id,
        fact_type=fact_type.lower(),
        subject_ciphertext=field_codec.encrypt_text(
            clinic_id, "fact_assertion.subject", assertion_id, subject
        ),
        normalized_value_ciphertext=field_codec.encrypt_text(
            clinic_id, "fact_assertion.normalized_value", assertion_id, normalized_value
        ),
        normalized_key_hash=hashlib.sha256(normalized_key.encode()).hexdigest(),
        polarity=polarity,
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
    assessment: DecisionAssessment | None, highlight: Highlight
) -> str:
    if highlight.unresolved:
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
) -> dict[str, object]:
    state = assessment_review_state(assessment, highlight)
    risk = {
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
    confidence = {
        "band": assessment.confidence_band if assessment else "not_applicable",
        "lower_bound": assessment.confidence_lower_bound if assessment else None,
        "calibration_report_id": str(assessment.calibration_report_id)
        if assessment and assessment.calibration_report_id
        else None,
    }
    return {
        "review_state": state,
        "risk": risk,
        "confidence": confidence,
        "importance": {
            "score": highlight.final_score,
            "components": score_components,
            "protected": bool(
                highlight.critical
                or highlight.unresolved
                or highlight.clinician_confirmed
            ),
        },
        "abstention_reason": assessment.abstention_reason if assessment else None,
    }
