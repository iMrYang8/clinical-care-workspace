from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.models import (
    Clinic,
    Highlight,
    ImportanceFeatureStat,
    ImportanceFeedbackEvent,
    get_datetime_utc,
)

MIN_FEATURE_WEIGHT = -0.20
MAX_FEATURE_WEIGHT = 0.20
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
    risk_reason: str
    components: dict[str, float]


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
    if (critical or unresolved or clinician_confirmed) and learned < 0:
        learned = 0.0
    final = max(0.0, min(1.0, base + learned))
    if critical:
        reason = "critical"
    elif unresolved:
        reason = "unresolved"
    elif clinician_confirmed:
        reason = "clinician_confirmed"
    elif clinical_entity:
        reason = "clinical_entity"
    elif learned > 0:
        reason = "clinic_feedback"
    else:
        reason = "recency"
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


def refresh_highlight_score(session: Session, highlight: Highlight) -> ScoreResult:
    now = datetime.now(UTC)
    created_at = highlight.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - created_at).total_seconds() / 86_400)
    feature_keys = sanitize_feature_keys(highlight.feature_keys_json)
    highlight.feature_keys_json = feature_keys
    score = calculate_score(
        critical=highlight.critical,
        unresolved=highlight.unresolved,
        clinician_confirmed=highlight.clinician_confirmed,
        feature_keys=feature_keys,
        feature_weights=_weights_for(session, highlight.clinic_id, feature_keys),
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
    highlight: Highlight, signal: str, idempotency_key: str
) -> tuple[list[str], str, str]:
    if signal not in SIGNAL_DELTAS:
        raise ValueError("Unsupported importance signal")
    feature_keys = sanitize_feature_keys(highlight.feature_keys_json)
    request_hash = hashlib.sha256(
        (f"{highlight.id}:{signal}:" + ",".join(sorted(feature_keys))).encode()
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
) -> bool:
    """Validate a feedback key and report an already-applied request.

    The caller must hold the clinic serialization lock. This check deliberately
    also runs before no-op state transitions, so a key used for another target
    or signal cannot bypass request binding merely because the target is already
    pinned, accepted, rejected, or dismissed.
    """

    _, request_hash, idempotency_token = _feedback_request_identity(
        highlight, signal, idempotency_key
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
        highlight, signal, idempotency_key
    )
    if feedback_idempotency_replayed(
        session,
        context,
        highlight,
        signal=signal,
        idempotency_key=idempotency_key,
    ):
        return False, set()
    if not settings.IMPORTANCE_LEARNING_ENABLED:
        refresh_highlight_score(session, highlight)
        return False, {highlight.patient_id}

    delta = SIGNAL_DELTAS[signal]
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
            feature_keys_json=feature_keys,
            applied_delta=delta,
            idempotency_key=idempotency_token,
            request_sha256=request_hash,
        )
    )
    session.flush()

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
