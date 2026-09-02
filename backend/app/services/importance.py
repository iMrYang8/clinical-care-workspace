from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from sqlmodel import Session, col, desc, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.models import (
    Clinic,
    Highlight,
    ImportanceCandidateExposure,
    ImportanceCandidateSet,
    ImportanceExposureQualificationReport,
    ImportanceFeatureStat,
    ImportanceFeedbackEvent,
    PatientGlanceSnapshot,
    RiskReason,
    get_datetime_utc,
)

MIN_FEATURE_WEIGHT = -0.20
MAX_FEATURE_WEIGHT = 0.20
IMPORTANCE_EXPOSURE_REPORT_VERSION = "importance-exposure-recall-v1"
IMPORTANCE_EXPOSURE_REPORT_WINDOW_HOURS = 24
IMPORTANCE_EXPOSURE_REPORT_VALID_HOURS = 24
SIGNAL_DELTAS = {
    "pin": 0.08,
    "accept": 0.06,
    "manual": 0.06,
    "comment": 0.02,
    "edit": 0.01,
    "reject": -0.08,
    "dismiss": -0.04,
}

_SAFE_EXACT_FEATURES = {
    "entity:allergy",
    "entity:medication",
    "entity:diagnosis",
    "topic:follow_up",
    "risk:critical",
}
_ENTRY_TYPE = re.compile(r"^entry_type:[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class ScoreResult:
    base: float
    learned: float
    final: float
    risk_reason: RiskReason
    components: dict[str, float]


@dataclass(frozen=True)
class ImportanceModeQualification:
    configured_mode: Literal["disabled", "shadow", "active"]
    effective_mode: Literal["disabled", "shadow", "active"]
    report: ImportanceExposureQualificationReport | None
    current: bool
    reasons: tuple[str, ...]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def latest_importance_exposure_report(
    session: Session, clinic_id: uuid.UUID
) -> ImportanceExposureQualificationReport | None:
    return session.exec(
        select(ImportanceExposureQualificationReport)
        .where(ImportanceExposureQualificationReport.clinic_id == clinic_id)
        .order_by(
            desc(col(ImportanceExposureQualificationReport.created_at)),
            desc(col(ImportanceExposureQualificationReport.id)),
        )
    ).first()


def importance_report_current_reasons(
    report: ImportanceExposureQualificationReport,
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    current_time = _utc(now or get_datetime_utc())
    reasons: list[str] = []
    if report.report_version != IMPORTANCE_EXPOSURE_REPORT_VERSION:
        reasons.append("importance_exposure_report_version_mismatch")
    if not report.qualified:
        reasons.append("importance_exposure_report_not_qualified")
        reasons.extend(report.qualification_reasons_json)
    if _utc(report.expires_at) <= current_time:
        reasons.append("importance_exposure_report_expired")
    if _utc(report.window_end) > current_time + timedelta(minutes=5):
        reasons.append("importance_exposure_report_future_window")
    expected_window = timedelta(hours=IMPORTANCE_EXPOSURE_REPORT_WINDOW_HOURS)
    if abs((_utc(report.window_end) - _utc(report.window_start)) - expected_window) > (
        timedelta(seconds=1)
    ):
        reasons.append("importance_exposure_report_window_mismatch")
    return tuple(dict.fromkeys(reasons))


def qualify_importance_mode(
    session: Session,
    clinic_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ImportanceModeQualification:
    """Resolve the effective mode; active always fails closed to shadow."""

    configured = settings.IMPORTANCE_LEARNING_MODE
    if configured != "active":
        return ImportanceModeQualification(
            configured_mode=configured,
            effective_mode=configured,
            report=None,
            current=True,
            reasons=(),
        )
    report = latest_importance_exposure_report(session, clinic_id)
    reasons: list[str] = []
    if report is None:
        reasons.append("importance_exposure_report_missing")
    else:
        reasons.extend(importance_report_current_reasons(report, now=now))
    unique_reasons = tuple(dict.fromkeys(reasons))
    return ImportanceModeQualification(
        configured_mode=configured,
        effective_mode="active" if not unique_reasons else "shadow",
        report=report,
        current=not unique_reasons,
        reasons=unique_reasons,
    )


def _candidate_set_surface_counts(
    candidate_set: ImportanceCandidateSet, surface: str
) -> tuple[int, int, int, int]:
    if surface == "current_priorities":
        return (
            candidate_set.current_priorities_candidate_count,
            candidate_set.current_priorities_displayed_count,
            candidate_set.current_priorities_protected_candidate_count,
            candidate_set.current_priorities_ordinary_candidate_count,
        )
    return (
        candidate_set.clinical_review_candidate_count,
        candidate_set.clinical_review_displayed_count,
        candidate_set.clinical_review_protected_candidate_count,
        candidate_set.clinical_review_ordinary_candidate_count,
    )


def generate_importance_exposure_report(
    session: Session,
    *,
    clinic_id: uuid.UUID,
    generated_by_membership_id: uuid.UUID,
    window_hours: int = IMPORTANCE_EXPOSURE_REPORT_WINDOW_HOURS,
    now: datetime | None = None,
) -> ImportanceExposureQualificationReport:
    """Persist a complete, per-surface recall audit over declared sets."""

    current_time = _utc(now or get_datetime_utc())
    window_start = current_time - timedelta(hours=window_hours)
    candidate_sets = list(
        session.exec(
            select(ImportanceCandidateSet).where(
                ImportanceCandidateSet.clinic_id == clinic_id,
                ImportanceCandidateSet.observed_at >= window_start,
                ImportanceCandidateSet.observed_at <= current_time,
            )
        ).all()
    )
    candidate_set_ids = [item.candidate_set_id for item in candidate_sets]
    exposures: list[ImportanceCandidateExposure] = []
    if candidate_set_ids:
        exposures = list(
            session.exec(
                select(ImportanceCandidateExposure).where(
                    ImportanceCandidateExposure.clinic_id == clinic_id,
                    col(ImportanceCandidateExposure.candidate_set_id).in_(
                        candidate_set_ids
                    ),
                )
            ).all()
        )
    exposures_by_set: dict[str, list[ImportanceCandidateExposure]] = {}
    for exposure in exposures:
        exposures_by_set.setdefault(exposure.candidate_set_id, []).append(exposure)

    surfaces: dict[str, dict[str, int | float]] = {}
    total_missing = 0
    total_duplicate = 0
    reasons: list[str] = []
    for surface in ("current_priorities", "clinical_review"):
        expected_candidates = 0
        expected_displayed = 0
        expected_protected = 0
        expected_ordinary = 0
        actual_rows: list[ImportanceCandidateExposure] = []
        surface_missing = 0
        surface_duplicate = 0
        for candidate_set in candidate_sets:
            expected, displayed, protected, ordinary = _candidate_set_surface_counts(
                candidate_set, surface
            )
            expected_candidates += expected
            expected_displayed += displayed
            expected_protected += protected
            expected_ordinary += ordinary
            rows = [
                row
                for row in exposures_by_set.get(candidate_set.candidate_set_id, [])
                if row.surface == surface
            ]
            actual_rows.extend(rows)
            unique_highlights = {row.highlight_id for row in rows}
            surface_missing += max(0, expected - len(unique_highlights))
            surface_duplicate += max(0, len(unique_highlights) - expected)
            surface_duplicate += max(0, len(rows) - len(unique_highlights))

            rank_counts: dict[int, int] = {}
            for row in rows:
                rank_counts[row.rank] = rank_counts.get(row.rank, 0) + 1
            surface_duplicate += sum(
                count - 1 for count in rank_counts.values() if count > 1
            )
            actual_displayed = sum(1 for row in rows if row.displayed)
            surface_missing += max(0, displayed - actual_displayed)
            surface_duplicate += max(0, actual_displayed - displayed)
            actual_protected = sum(1 for row in rows if row.protected)
            actual_ordinary = len(rows) - actual_protected
            surface_missing += max(0, protected - actual_protected)
            surface_duplicate += max(0, actual_protected - protected)
            surface_missing += max(0, ordinary - actual_ordinary)
            surface_duplicate += max(0, actual_ordinary - ordinary)

        actual_displayed = sum(1 for row in actual_rows if row.displayed)
        actual_protected = sum(1 for row in actual_rows if row.protected)
        actual_protected_displayed = sum(
            1 for row in actual_rows if row.protected and row.displayed
        )
        actual_ordinary = len(actual_rows) - actual_protected
        actual_ordinary_displayed = sum(
            1 for row in actual_rows if not row.protected and row.displayed
        )
        surfaces[surface] = {
            "candidate_count": expected_candidates,
            "telemetry_count": len(actual_rows),
            "displayed_count": actual_displayed,
            "protected_candidate_count": expected_protected,
            "protected_displayed_count": actual_protected_displayed,
            "ordinary_candidate_count": expected_ordinary,
            "ordinary_displayed_count": actual_ordinary_displayed,
            "missing_telemetry_count": surface_missing,
            "duplicate_telemetry_count": surface_duplicate,
        }
        total_missing += surface_missing
        total_duplicate += surface_duplicate
        if expected_candidates == 0:
            reasons.append(f"{surface}_surface_empty")
        if surface_missing:
            reasons.append(f"{surface}_telemetry_missing")
        if surface_duplicate:
            reasons.append(f"{surface}_telemetry_duplicate")

    candidate_count = sum(item.total_candidate_count for item in candidate_sets)
    displayed_count = sum(1 for item in exposures if item.displayed)
    protected_candidate_count = sum(
        item.protected_candidate_count for item in candidate_sets
    )
    ordinary_candidate_count = sum(
        item.ordinary_candidate_count for item in candidate_sets
    )
    protected_displayed_count = sum(
        1 for item in exposures if item.protected and item.displayed
    )
    ordinary_displayed_count = sum(
        1 for item in exposures if not item.protected and item.displayed
    )
    actual_ordinary_count = sum(1 for item in exposures if not item.protected)
    protected_recall = (
        min(1.0, protected_displayed_count / protected_candidate_count)
        if protected_candidate_count
        else 0.0
    )
    ordinary_recall = (
        min(1.0, actual_ordinary_count / ordinary_candidate_count)
        if ordinary_candidate_count
        else 0.0
    )
    ordinary_exposure_rate = (
        min(1.0, ordinary_displayed_count / ordinary_candidate_count)
        if ordinary_candidate_count
        else 0.0
    )
    if not candidate_sets:
        reasons.append("candidate_sets_missing")
    if protected_candidate_count == 0:
        reasons.append("protected_candidates_missing")
    elif protected_recall < 1.0:
        reasons.append("protected_recall_incomplete")
    if ordinary_candidate_count == 0:
        reasons.append("ordinary_candidates_missing")
    elif ordinary_recall < 1.0:
        reasons.append("ordinary_recall_incomplete")
    if total_missing:
        reasons.append("candidate_telemetry_missing")
    if total_duplicate:
        reasons.append("candidate_telemetry_duplicate")
    unique_reasons = list(dict.fromkeys(reasons))
    report = ImportanceExposureQualificationReport(
        clinic_id=clinic_id,
        report_version=IMPORTANCE_EXPOSURE_REPORT_VERSION,
        window_start=window_start,
        window_end=current_time,
        source_candidate_set_count=len(candidate_sets),
        candidate_count=candidate_count,
        telemetry_count=len(exposures),
        displayed_count=displayed_count,
        protected_candidate_count=protected_candidate_count,
        protected_displayed_count=protected_displayed_count,
        ordinary_candidate_count=ordinary_candidate_count,
        ordinary_displayed_count=ordinary_displayed_count,
        protected_recall=protected_recall,
        ordinary_recall=ordinary_recall,
        ordinary_exposure_rate=ordinary_exposure_rate,
        missing_telemetry_count=total_missing,
        duplicate_telemetry_count=total_duplicate,
        surface_metrics_json=surfaces,
        qualified=not unique_reasons,
        qualification_reasons_json=unique_reasons,
        generated_by_membership_id=generated_by_membership_id,
        expires_at=current_time
        + timedelta(hours=IMPORTANCE_EXPOSURE_REPORT_VALID_HOURS),
        created_at=current_time,
    )
    session.add(report)
    session.flush()
    report_current = not importance_report_current_reasons(report, now=current_time)
    projected_mode: Literal["disabled", "shadow", "active"] = (
        "active"
        if settings.IMPORTANCE_LEARNING_MODE == "active" and report_current
        else (
            "disabled" if settings.IMPORTANCE_LEARNING_MODE == "disabled" else "shadow"
        )
    )
    for snapshot in session.exec(
        select(PatientGlanceSnapshot).where(
            PatientGlanceSnapshot.clinic_id == clinic_id
        )
    ).all():
        snapshot.importance_mode = projected_mode
        snapshot.importance_qualification_report_id = (
            report.id if report_current else None
        )
        snapshot.importance_qualification_report_version = (
            report.report_version if report_current else None
        )
        snapshot.importance_qualification_expires_at = (
            report.expires_at if report_current else None
        )
        session.add(snapshot)
    return report


def is_safety_protected(
    highlight: Highlight, *, effective_critical: bool | None = None
) -> bool:
    """Return the server-authoritative safety-queue classification.

    Allergy evidence is protected even when it is not itself marked critical:
    ranking feedback must never suppress an active allergy assertion.
    """

    feature_keys = sanitize_feature_keys(highlight.feature_keys_json)
    return bool(
        (highlight.critical if effective_critical is None else effective_critical)
        or highlight.pinned
        or highlight.unresolved
        or highlight.review_required
        or highlight.support_review_required
        or highlight.clinician_confirmed
        or "entity:allergy" in feature_keys
    )


def sanitize_feature_keys(feature_keys: list[str]) -> list[str]:
    """Keep bounded taxonomy tokens and reject free text/identity material."""

    output: list[str] = []
    for raw in feature_keys:
        key = raw.strip().lower()
        if key in _SAFE_EXACT_FEATURES or _ENTRY_TYPE.fullmatch(key):
            if key not in output:
                output.append(key)
        if len(output) == 10:
            break
    return output


def apply_weight_delta(weight: float, delta: float, *, observations: int) -> float:
    updated = weight + delta / math.sqrt(1 + max(0, observations))
    return round(max(MIN_FEATURE_WEIGHT, min(MAX_FEATURE_WEIGHT, updated)), 10)


def calculate_score(
    *,
    critical: bool,
    unresolved: bool,
    clinician_confirmed: bool,
    feature_keys: list[str],
    feature_weights: dict[str, float],
    age_days: float,
) -> ScoreResult:
    risk = 1.0 if critical else 0.0
    unresolved_component = 1.0 if unresolved else 0.0
    clinical_entity = (
        1.0 if any(key.startswith("entity:") for key in feature_keys) else 0.0
    )
    confirmed = 1.0 if clinician_confirmed else 0.0
    recency = math.exp(-max(age_days, 0.0) / 90.0)
    base = (
        0.30 * risk
        + 0.20 * unresolved_component
        + 0.15 * clinical_entity
        + 0.15 * confirmed
        + 0.20 * recency
    )
    weights = [feature_weights.get(key, 0.0) for key in feature_keys]
    learned = sum(weights) / len(weights) if weights else 0.0
    learned = max(MIN_FEATURE_WEIGHT, min(MAX_FEATURE_WEIGHT, learned))
    allergy_evidence = "entity:allergy" in feature_keys
    if (
        critical or unresolved or clinician_confirmed or allergy_evidence
    ) and learned < 0:
        learned = 0.0
    final = max(0.0, min(1.0, base + learned))
    if critical:
        reason = RiskReason.CRITICAL
    elif unresolved:
        reason = RiskReason.UNRESOLVED
    elif clinician_confirmed:
        reason = RiskReason.CLINICIAN_CONFIRMED
    elif clinical_entity:
        reason = RiskReason.CLINICAL_ENTITY
    elif learned > 0:
        reason = RiskReason.CLINIC_FEEDBACK
    else:
        reason = RiskReason.RECENCY
    return ScoreResult(
        base=round(base, 6),
        learned=round(learned, 6),
        final=round(final, 6),
        risk_reason=reason,
        components={
            "risk": round(0.30 * risk, 6),
            "unresolved": round(0.20 * unresolved_component, 6),
            "clinical_entity": round(0.15 * clinical_entity, 6),
            "clinician_confirmed": round(0.15 * confirmed, 6),
            "recency": round(0.20 * recency, 6),
            "learned": round(learned, 6),
            "final": round(final, 6),
        },
    )


def _weights_for(
    session: Session, clinic_id: uuid.UUID, feature_keys: list[str]
) -> dict[str, float]:
    if not feature_keys:
        return {}
    stats = session.exec(
        select(ImportanceFeatureStat).where(
            ImportanceFeatureStat.clinic_id == clinic_id,
            col(ImportanceFeatureStat.feature_key).in_(feature_keys),
        )
    ).all()
    return {item.feature_key: item.weight for item in stats}


def refresh_highlight_score(
    session: Session,
    highlight: Highlight,
    *,
    importance_mode: Literal["disabled", "shadow", "active"] | None = None,
) -> ScoreResult:
    now = datetime.now(UTC)
    created_at = highlight.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - created_at).total_seconds() / 86_400)
    feature_keys = sanitize_feature_keys(highlight.feature_keys_json)
    highlight.feature_keys_json = feature_keys
    effective_mode = (
        importance_mode
        or qualify_importance_mode(session, highlight.clinic_id).effective_mode
    )
    score = calculate_score(
        critical=highlight.critical,
        unresolved=highlight.unresolved,
        clinician_confirmed=highlight.clinician_confirmed,
        feature_keys=feature_keys,
        feature_weights=(
            _weights_for(session, highlight.clinic_id, feature_keys)
            if effective_mode == "active"
            else {}
        ),
        age_days=age_days,
    )
    highlight.base_score = score.base
    highlight.learned_score = score.learned
    highlight.final_score = score.final
    highlight.risk_reason = score.risk_reason
    session.add(highlight)
    return score


def lock_importance_scope(session: Session, clinic_id: uuid.UUID) -> None:
    """Serialize clinic-scoped feedback before locking or mutating a highlight."""

    # Keep a single lock order everywhere: clinic first, then highlight/stat rows.
    # Suppressing autoflush is essential when callers have already staged a
    # highlight mutation (for example comment/edit-derived weak feedback).
    with session.no_autoflush:
        session.exec(
            select(Clinic).where(Clinic.id == clinic_id).with_for_update()
        ).one()


def _feedback_request_identity(
    highlight: Highlight, signal: str, idempotency_key: str, reason: str | None = None
) -> tuple[list[str], str, str]:
    if signal not in SIGNAL_DELTAS:
        raise ValueError("Unsupported importance signal")
    feature_keys = sanitize_feature_keys(highlight.feature_keys_json)
    request_hash = hashlib.sha256(
        (
            f"{highlight.id}:{signal}:{reason or ''}:" + ",".join(sorted(feature_keys))
        ).encode()
    ).hexdigest()
    idempotency_token = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return feature_keys, request_hash, idempotency_token


def feedback_idempotency_replayed(
    session: Session,
    context: RequestContext,
    highlight: Highlight,
    *,
    signal: str,
    idempotency_key: str,
    reason: str | None = None,
) -> bool:
    """Validate a feedback key and report an already-applied request.

    The caller must hold the clinic serialization lock. This check deliberately
    also runs before no-op state transitions, so a key used for another target
    or signal cannot bypass request binding merely because the target is already
    pinned, accepted, rejected, or dismissed.
    """

    _, request_hash, idempotency_token = _feedback_request_identity(
        highlight, signal, idempotency_key, reason
    )
    existing = session.exec(
        select(ImportanceFeedbackEvent).where(
            ImportanceFeedbackEvent.clinic_id == context.clinic_id,
            ImportanceFeedbackEvent.idempotency_key == idempotency_token,
        )
    ).first()
    if existing is None:
        return False
    if (
        existing.request_sha256 != request_hash
        or existing.highlight_id != highlight.id
        or existing.signal != signal
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": "The key was already used for different feedback",
            },
        )
    return True


def record_feedback(
    session: Session,
    context: RequestContext,
    highlight: Highlight,
    *,
    signal: str,
    idempotency_key: str,
    reason: str | None = None,
    learn: bool = True,
) -> tuple[bool, set[uuid.UUID]]:
    # The clinic row is the serialization point for learning. It makes first
    # observation creation and idempotency replay atomic without introducing a
    # global lock across tenants.
    # Do not autoflush a just-mutated highlight before taking the clinic lock:
    # two concurrent feedback requests would otherwise each hold a highlight
    # row while waiting on the other's clinic/candidate work.
    lock_importance_scope(session, context.clinic_id)
    # Comment/edit callers may have loaded this object before waiting for the
    # clinic lock. Re-lock and refresh it so a concurrently committed
    # accept/pin transition cannot be overwritten by stale score/glance data.
    highlight = session.exec(
        select(Highlight)
        .where(
            Highlight.clinic_id == context.clinic_id,
            Highlight.id == highlight.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    feature_keys, request_hash, idempotency_token = _feedback_request_identity(
        highlight, signal, idempotency_key, reason
    )
    if feedback_idempotency_replayed(
        session,
        context,
        highlight,
        signal=signal,
        idempotency_key=idempotency_key,
        reason=reason,
    ):
        return False, set()
    learning_mode = qualify_importance_mode(session, context.clinic_id).effective_mode
    # Shadow mode is the safe default: it captures the same complete feedback
    # telemetry as an active rollout but does not mutate weights or counters.
    allergy_evidence = "entity:allergy" in feature_keys
    # Allergy feedback remains telemetry-only. Clinical removal must happen by
    # correcting or superseding the assertion, never by mutating rank weights.
    effective_learn = learn and not allergy_evidence
    delta = (
        SIGNAL_DELTAS[signal] if effective_learn and learning_mode == "active" else 0.0
    )
    if learning_mode == "active" and effective_learn:
        for feature_key in feature_keys:
            stat = session.exec(
                select(ImportanceFeatureStat)
                .where(
                    ImportanceFeatureStat.clinic_id == context.clinic_id,
                    ImportanceFeatureStat.feature_key == feature_key,
                )
                .with_for_update()
            ).first()
            if stat is None:
                stat = ImportanceFeatureStat(
                    clinic_id=context.clinic_id,
                    feature_key=feature_key,
                )
            stat.weight = apply_weight_delta(
                stat.weight, delta, observations=stat.observation_count
            )
            stat.observation_count += 1
            if delta > 0:
                stat.positive_count += 1
            elif delta < 0:
                stat.negative_count += 1
            stat.updated_at = get_datetime_utc()
            session.add(stat)

    session.add(
        ImportanceFeedbackEvent(
            clinic_id=context.clinic_id,
            highlight_id=highlight.id,
            actor_membership_id=context.membership.id,
            signal=signal,
            reason=reason,
            feature_keys_json=feature_keys,
            applied_delta=delta,
            idempotency_key=idempotency_token,
            request_sha256=request_hash,
        )
    )
    session.flush()

    if learning_mode != "active":
        refresh_highlight_score(session, highlight)
        return True, {highlight.patient_id}

    affected_patients: set[uuid.UUID] = set()
    candidates = session.exec(
        select(Highlight)
        .where(Highlight.clinic_id == context.clinic_id)
        .execution_options(populate_existing=True)
    ).all()
    affected_features = set(feature_keys)
    for candidate in candidates:
        if candidate.id == highlight.id or affected_features.intersection(
            sanitize_feature_keys(candidate.feature_keys_json)
        ):
            refresh_highlight_score(session, candidate)
            affected_patients.add(candidate.patient_id)
    return True, affected_patients
