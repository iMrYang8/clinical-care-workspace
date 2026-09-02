from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Awaitable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Literal, TypeVar, cast

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.db import set_rls_clinic
from app.core.field_crypto import field_codec
from app.models import (
    AIRun,
    AIRunPublic,
    ClinicalFactAssertion,
    ClinicMembership,
    ClinicOperationalSetting,
    ConflictCase,
    DecisionAssessment,
    Entry,
    EntryVersion,
    Highlight,
    InteractionType,
    Job,
    JobAttempt,
    JobPublic,
    JobRetryAttemptPublic,
    Patient,
    ProvenancePointer,
    ProviderCircuitPublic,
    ProviderCircuitState,
    RedactionRun,
    User,
    get_datetime_utc,
)
from app.services.clinic_ai_settings import clinic_ai_runtime
from app.services.conflicts import NormalizedFact, extract_normalized_facts
from app.services.decisioning import (
    ai_assessment_coverage,
    create_assertion,
    deterministic_risk,
    evaluation_manifest_sha256,
    matching_calibration_report,
    redaction_is_qualified,
    requalify_assessment_confidence,
)
from app.services.egress import TextModelEgressGateway
from app.services.importance import refresh_highlight_score, sanitize_feature_keys
from app.services.nightingale import (
    decrypt_version,
    emit_change,
    get_patient,
    rebuild_glance,
)
from app.services.provider_resilience import (
    ProviderCircuitOpen,
    ProviderFailure,
    classify_provider_failure,
    retry_delay_seconds,
)
from app.services.providers.base import (
    ClinicalFact,
    ClinicalNoteDraft,
    ExtractionContext,
    validate_evidence,
)
from app.services.providers.deterministic import DeterministicClinicalNoteProvider
from app.services.providers.openai_text import OpenAITextProvider
from app.services.redaction import ClinicalScribePipeline, RedactionService

_HIGH_RISK_TEXT = re.compile(
    r"\b(?:anaphylaxis|anaphylactic|suicid(?:e|al)|chest pain|stroke|"
    r"sepsis|critical|emergency|overdose)\b",
    re.IGNORECASE,
)
_INTERACTION_TYPES: frozenset[str] = frozenset(
    {"care_note", "doctor_consult", "patient_insight", "voice_session"}
)
_WARNING_CODES: frozenset[str] = frozenset(
    {
        "HIGH_RISK_REVIEW_FAILED",
        "HIGH_RISK_REVIEW_MODEL_UNAVAILABLE",
        "INVALID_EVIDENCE_SPAN",
        "INVALID_RAW_EVIDENCE_SPAN",
        "NO_STRUCTURED_FACTS",
        "PRESIDIO_UNAVAILABLE",
        "REDACTION_EVALUATION_REQUIRED",
        "PROVIDER_FACT_SCHEMA_INVALID",
        "PROVIDER_REPORTED_WARNING",
        "PROVIDER_WARNING_SCHEMA_INVALID",
        "REDACTION_REVIEW",
        "RESIDUAL_PHI_DETECTED",
    }
)

_T = TypeVar("_T")


class _InternalJobError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _TextJobDeadline:
    """One monotonic deadline shared by every text-model stage in a job.

    Stage-specific limits remain upper bounds.  For example, extraction gets
    at most the 15-second first-result window and review gets at most the
    30-second remote-request window, but each also receives no more than the
    time left in the 75-second whole-job budget.
    """

    def __init__(self, timeout_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._deadline = loop.time() + max(0.0, timeout_seconds)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._loop.time())

    async def wait_for(
        self,
        awaitable: Awaitable[_T],
        *,
        stage_timeout_seconds: float | None = None,
    ) -> _T:
        timeout = self.remaining_seconds
        if stage_timeout_seconds is not None:
            timeout = min(timeout, max(0.0, stage_timeout_seconds))
        return await asyncio.wait_for(awaitable, timeout=timeout)

    def raise_if_expired(self) -> None:
        if self.remaining_seconds <= 0:
            raise TimeoutError("AI_TEXT_JOB_TIMEOUT")


def _safe_warning_codes(values: list[str]) -> list[str]:
    return sorted(
        {
            value if value in _WARNING_CODES else "PROVIDER_REPORTED_WARNING"
            for value in values
        }
    )


def _trusted_interaction_type(payload: dict[str, Any]) -> InteractionType:
    candidate = payload.get("interaction_type", "care_note")
    if not isinstance(candidate, str) or candidate not in _INTERACTION_TYPES:
        raise _InternalJobError("INVALID_INTERACTION_TYPE")
    return cast(InteractionType, candidate)


def canonical_request_hash(
    patient_id: uuid.UUID, kind: str, payload: dict[str, Any]
) -> str:
    encoded = json.dumps(
        {"patient_id": str(patient_id), "kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _map_facts_to_source(
    facts: list[ClinicalFact], source_text: str
) -> tuple[list[ClinicalFact], bool]:
    """Remote offsets target redacted text; only exact raw-source matches survive."""

    mapped: list[ClinicalFact] = []
    discarded = False
    for fact in facts:
        starts = []
        cursor = 0
        while True:
            index = source_text.find(fact.evidence_quote, cursor)
            if index < 0:
                break
            starts.append(index)
            cursor = index + max(1, len(fact.evidence_quote))
        if len(starts) != 1:
            discarded = True
            continue
        start = starts[0]
        mapped.append(
            replace(
                fact,
                evidence_start=start,
                evidence_end=start + len(fact.evidence_quote),
            )
        )
    return mapped, discarded


def _configured_remote_provider(
    session: Session, clinic_id: uuid.UUID
) -> OpenAITextProvider | None:
    operational = session.exec(
        select(ClinicOperationalSetting).where(
            ClinicOperationalSetting.clinic_id == clinic_id
        )
    ).first()
    # Remote text egress is a clinic-level opt-in in addition to deployment
    # configuration.  Missing settings fail closed so onboarding must make the
    # boundary explicit before credentials or a remote transport are selected.
    if operational is None or not operational.remote_text_egress_enabled:
        return None
    runtime = clinic_ai_runtime(session, clinic_id)
    if (
        settings.AI_PROVIDER != "openai"
        or not settings.REMOTE_TEXT_EGRESS_ENABLED
        or not runtime.api_key
        or not runtime.fast_model
    ):
        return None
    return OpenAITextProvider(
        api_key=runtime.api_key,
        extract_model=runtime.fast_model,
        review_model=runtime.careful_model,
        timeout_seconds=settings.REMOTE_REQUEST_TIMEOUT_SECONDS,
        connect_timeout_seconds=settings.REMOTE_CONNECT_TIMEOUT_SECONDS,
    )


def _ai_run_public(run: AIRun) -> AIRunPublic:
    return AIRunPublic(
        id=run.id,
        patient_id=run.patient_id,
        source_entry_version_id=run.source_entry_version_id,
        provider=run.provider,
        model=run.model,
        review_model=run.review_model,
        review_status=run.review_status,
        status=run.status,
        risk_tier=run.risk_tier,
        fallback_reason=run.fallback_reason,
        needs_review=run.needs_review,
        output_entry_id=run.output_entry_id,
        output_entry_version_id=run.output_entry_version_id,
        warnings=run.warnings_json,
        created_at=run.created_at,
    )


def _retry_history_public(
    rows: list[dict[str, object]],
) -> tuple[list[JobRetryAttemptPublic], int]:
    """Return only typed, PHI-free retry attempts from persisted legacy JSON."""

    attempts: list[JobRetryAttemptPublic] = []
    invalid_count = 0
    for row in rows:
        try:
            attempts.append(JobRetryAttemptPublic.model_validate(row))
        except (TypeError, ValueError):
            invalid_count += 1
    return attempts, invalid_count


def _provider_circuit_public(
    circuit: ProviderCircuitState | None,
) -> ProviderCircuitPublic | None:
    if circuit is None:
        return None
    return ProviderCircuitPublic(
        provider=circuit.provider,
        capability=circuit.capability,
        state=cast(Literal["closed", "open", "half_open"], circuit.state),
        consecutive_failures=circuit.consecutive_failures,
        last_error_class=circuit.last_error_class,
        opened_at=circuit.opened_at,
        next_probe_at=circuit.next_probe_at,
        last_success_at=circuit.last_success_at,
        updated_at=circuit.updated_at,
    )


def _job_confidence(
    session: Session,
    job: Job,
    run: AIRun | None,
) -> tuple[Literal["qualified", "unavailable", "review_required"], list[str], bool]:
    """Requalify every model-derived job output at read time.

    Job rows are durable operational records.  A once-valid calibration report
    may expire or become inconsistent after the run, so persisted confidence is
    never trusted as a current claim.
    """

    if run is None:
        unavailable_reasons = ["JOB_OUTPUT_NOT_AVAILABLE"]
        safety_review_required = bool(
            job.state not in {"pending", "running"}
            or job.timed_out_at is not None
            or job.provider_outage
        )
        return "unavailable", unavailable_reasons, safety_review_required

    assessments = session.exec(
        select(DecisionAssessment)
        .join(Highlight, col(DecisionAssessment.highlight_id) == col(Highlight.id))
        .where(
            DecisionAssessment.clinic_id == job.clinic_id,
            Highlight.clinic_id == job.clinic_id,
            Highlight.patient_id == job.patient_id,
            Highlight.source_entry_version_id == run.source_entry_version_id,
            col(Highlight.candidate_fingerprint).is_not(None),
        )
    ).all()
    if not assessments:
        return (
            "review_required",
            ["JOB_CONFIDENCE_ASSESSMENT_UNAVAILABLE"],
            True,
        )
    # Enumerating assessments cannot see a model-derived highlight that never
    # received one, so the expected population is queried independently. A
    # partially assessed job is not a qualified job.
    coverage = ai_assessment_coverage(
        session,
        clinic_id=job.clinic_id,
        patient_id=job.patient_id,
        source_entry_version_id=run.source_entry_version_id,
    )
    if not coverage.complete:
        return (
            "review_required",
            ["JOB_CONFIDENCE_ASSESSMENT_INCOMPLETE"],
            True,
        )

    reasons: list[str] = []
    all_qualified = True
    for assessment in assessments:
        qualification = requalify_assessment_confidence(
            session,
            assessment,
            provider=run.provider,
            exact_model_id=run.model,
        )
        if not qualification.qualified:
            all_qualified = False
            reasons.extend(qualification.reasons)
        if assessment.abstained:
            all_qualified = False
            reasons.append(
                assessment.abstention_reason or "ASSESSMENT_ABSTAINED_REVIEW_REQUIRED"
            )

    if run.needs_review or run.fallback_reason is not None or run.status != "completed":
        all_qualified = False
        reasons.append("AI_RUN_REVIEW_REQUIRED")

    unique_reasons = sorted(set(reasons))
    if all_qualified:
        return "qualified", [], False
    return "review_required", unique_reasons, True


def job_public(session: Session, job: Job) -> JobPublic:
    now = get_datetime_utc()
    run = session.exec(
        select(AIRun)
        .where(AIRun.clinic_id == job.clinic_id, AIRun.job_id == job.id)
        .order_by(col(AIRun.created_at).desc())
    ).first()
    circuit_capability = (
        "audio_transcription"
        if job.kind in {"voice_process", "voice_reanalyze"}
        else "clinical_text"
    )
    circuit = _provider_circuit(session, job.clinic_id, capability=circuit_capability)
    circuit_outage = bool(circuit is not None and circuit.state != "closed")
    # A legacy/mid-transaction failure may have persisted the job flag before
    # its circuit row. Keep that visible; a known closed circuit clears it.
    provider_outage = bool(job.provider_outage and (circuit is None or circuit_outage))
    outage_started_at = (
        circuit.opened_at
        if provider_outage and circuit is not None
        else job.delayed_at
        if provider_outage
        else None
    )
    outage_age_seconds = (
        max(0, int((now - outage_started_at).total_seconds()))
        if outage_started_at is not None
        else 0
    )
    retry_after_seconds = (
        max(0, math.ceil((job.next_run_at - now).total_seconds()))
        if job.next_run_at is not None and job.next_run_at > now
        else None
    )
    visible_state: str | None
    if job.state == "running":
        visible_state = "running"
    elif retry_after_seconds is not None:
        visible_state = "delayed"
    elif job.timed_out_at is not None:
        visible_state = "timed_out"
    elif job.state == "pending":
        visible_state = "queued"
    elif job.state == "failed":
        visible_state = "failed"
    else:
        visible_state = None
    retry_history, retry_history_invalid_count = _retry_history_public(
        job.retry_history_json
    )
    current_confidence_state, current_confidence_reasons, safety_review_required = (
        _job_confidence(session, job, run)
    )
    return JobPublic(
        id=job.id,
        patient_id=job.patient_id,
        kind=job.kind,
        state=job.state,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        error_code=job.error_code,
        ai_run=_ai_run_public(run) if run else None,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error_class=job.error_class,
        next_run_at=job.next_run_at,
        provider_outage=provider_outage,
        provider_circuit=_provider_circuit_public(circuit),
        retry_history=retry_history,
        retry_history_invalid_count=retry_history_invalid_count,
        delayed_at=job.delayed_at,
        timed_out_at=job.timed_out_at,
        last_attempt_at=job.last_attempt_at,
        outage_started_at=outage_started_at,
        outage_age_seconds=outage_age_seconds,
        retry_after_seconds=retry_after_seconds,
        visible_state=visible_state,
        current_confidence_state=current_confidence_state,
        current_confidence_reasons=current_confidence_reasons,
        safety_review_required=safety_review_required,
    )


def _provider_circuit(
    session: Session,
    clinic_id: uuid.UUID,
    *,
    capability: str = "clinical_text",
    lock: bool = False,
) -> ProviderCircuitState | None:
    statement = select(ProviderCircuitState).where(
        ProviderCircuitState.clinic_id == clinic_id,
        ProviderCircuitState.provider == "openai",
        ProviderCircuitState.capability == capability,
    )
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    return session.exec(statement).first()


def _assert_provider_circuit_available(session: Session, clinic_id: uuid.UUID) -> None:
    circuit = _provider_circuit(session, clinic_id, lock=True)
    if circuit is None or circuit.state == "closed":
        return
    now = get_datetime_utc()
    if circuit.next_probe_at is not None and circuit.next_probe_at > now:
        raise ProviderCircuitOpen("PROVIDER_CIRCUIT_OPEN")
    circuit.state = "half_open"
    circuit.updated_at = now
    session.add(circuit)
    session.flush()


def _record_provider_failure(
    session: Session,
    job: Job,
    failure: ProviderFailure,
    *,
    retry_index: int,
) -> datetime | None:
    now = get_datetime_utc()
    schedule_retry = failure.retryable and retry_index <= 5
    next_run_at = (
        now + timedelta(seconds=retry_delay_seconds(job.id, retry_index))
        if schedule_retry
        else None
    )
    circuit = _provider_circuit(session, job.clinic_id, lock=True)
    if circuit is None:
        candidate = ProviderCircuitState(
            clinic_id=job.clinic_id,
            provider="openai",
            capability="clinical_text",
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            circuit = candidate
        except IntegrityError:
            # A second job may create the clinic/provider circuit between the
            # initial read and insert. The unique row is the serialization
            # point; lock and update it rather than losing the job result.
            circuit = _provider_circuit(session, job.clinic_id, lock=True)
            if circuit is None:
                raise
    circuit.state = "open" if failure.retryable else "closed"
    circuit.consecutive_failures += 1
    circuit.last_error_class = failure.failure_class
    circuit.opened_at = circuit.opened_at or now
    circuit.next_probe_at = (
        next_run_at
        if next_run_at is not None
        else now + timedelta(seconds=3_600)
        if failure.retryable
        else None
    )
    circuit.updated_at = now
    session.add(circuit)
    job.error_code = failure.code
    job.error_class = failure.failure_class
    job.provider_outage = failure.retryable
    job.next_run_at = next_run_at
    job.delayed_at = now if schedule_retry else job.delayed_at
    if failure.failure_class == "timeout":
        job.timed_out_at = now
    history = list(job.retry_history_json)
    history.append(
        {
            "attempt": job.attempt_count,
            "error_code": failure.code,
            "error_class": failure.failure_class,
            "attempted_at": now.isoformat(),
            "next_retry_at": next_run_at.isoformat() if next_run_at else None,
        }
    )
    job.retry_history_json = history
    session.add(job)
    return next_run_at


def _record_provider_success(session: Session, job: Job) -> None:
    now = get_datetime_utc()
    circuit = _provider_circuit(session, job.clinic_id, lock=True)
    if circuit is not None:
        circuit.state = "closed"
        circuit.consecutive_failures = 0
        circuit.last_error_class = None
        circuit.opened_at = None
        circuit.next_probe_at = None
        circuit.last_success_at = now
        circuit.updated_at = now
        session.add(circuit)
    job.error_code = None
    job.error_class = None
    job.provider_outage = False
    job.next_run_at = None
    job.delayed_at = None
    session.add(job)


def get_scoped_job(
    session: Session,
    context: RequestContext,
    job_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Job:
    statement = select(Job).where(Job.clinic_id == context.clinic_id, Job.id == job_id)
    if lock:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    job = session.exec(statement).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    get_patient(session, context, job.patient_id)
    return job


def create_or_replay_job(
    session: Session,
    context: RequestContext,
    *,
    patient_id: uuid.UUID,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> tuple[Job, bool]:
    get_patient(session, context, patient_id)
    request_hash = canonical_request_hash(patient_id, kind, payload)
    idempotency_token = hashlib.sha256(idempotency_key.encode()).hexdigest()
    existing = session.exec(
        select(Job).where(
            Job.clinic_id == context.clinic_id,
            Job.kind == kind,
            Job.idempotency_key == idempotency_token,
        )
    ).first()
    if existing is not None:
        if existing.request_sha256 != request_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDEMPOTENCY_KEY_REUSED",
                    "message": "The key was already used for different input",
                },
            )
        return existing, True
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        clinic_id=context.clinic_id,
        patient_id=patient_id,
        kind=kind,
        idempotency_key=idempotency_token,
        request_sha256=request_hash,
        payload_ciphertext=field_codec.encrypt_json(
            context.clinic_id, "job.payload", job_id, payload
        ),
        created_by_id=context.user_id,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # Another transaction may have inserted the same idempotency tuple
        # after our first read. The unique constraint is the serialization
        # point; replay its durable result instead of surfacing a 500.
        existing = session.exec(
            select(Job).where(
                Job.clinic_id == context.clinic_id,
                Job.kind == kind,
                Job.idempotency_key == idempotency_token,
            )
        ).first()
        if existing is None:
            raise
        if existing.request_sha256 != request_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDEMPOTENCY_KEY_REUSED",
                    "message": "The key was already used for different input",
                },
            )
        return existing, True
    emit_change(
        session,
        context,
        action="job.created",
        resource_type="job",
        resource_id=job.id,
        metadata={"kind": kind, "state": "pending"},
    )
    return job, False


def worker_context_for_job(session: Session, job: Job) -> RequestContext | None:
    """Resolve a trusted active worker in the job's clinic.

    The requester's context is never reused to author system-derived data. The
    job binding is constructed server-side and rechecked again at claim time.
    """

    worker = session.exec(
        select(ClinicMembership, User)
        .join(User)
        .where(
            ClinicMembership.clinic_id == job.clinic_id,
            ClinicMembership.role == "worker",
            col(ClinicMembership.is_active).is_(True),
            col(User.is_active).is_(True),
        )
        .order_by(col(ClinicMembership.created_at), col(ClinicMembership.id))
        .execution_options(populate_existing=True)
    ).first()
    if worker is None:
        return None
    membership, user = worker
    return RequestContext(user=user, membership=membership, job_id=job.id)


def _trusted_patient_names(
    session: Session, context: RequestContext, patient_id: uuid.UUID
) -> list[str]:
    patient: Patient = get_patient(session, context, patient_id)
    display_name = field_codec.decrypt_text(
        patient.clinic_id,
        "patient.display_name",
        patient.id,
        patient.display_name_ciphertext,
    ).strip()
    return [display_name] if display_name else []


def _server_risk_flags(
    session: Session,
    context: RequestContext,
    patient_id: uuid.UUID,
    source_version: EntryVersion,
    source_text: str,
) -> tuple[bool, bool]:
    """Derive risk only from trusted server state and deterministic rules."""

    conflict_review = (
        session.exec(
            select(ConflictCase).where(
                ConflictCase.clinic_id == context.clinic_id,
                ConflictCase.status == "unresolved",
                (ConflictCase.left_entry_id == source_version.entry_id)
                | (ConflictCase.right_entry_id == source_version.entry_id),
            )
        ).first()
        is not None
    )
    protected_highlight = session.exec(
        select(Highlight).where(
            Highlight.clinic_id == context.clinic_id,
            Highlight.patient_id == patient_id,
            col(Highlight.critical).is_(True) | col(Highlight.unresolved).is_(True),
        )
    ).first()
    return (
        bool(_HIGH_RISK_TEXT.search(source_text)) or protected_highlight is not None,
        conflict_review,
    )


def _draft_payload(draft: ClinicalNoteDraft) -> dict[str, Any]:
    return {
        "summary": draft.summary,
        "facts": [
            {
                "fact_type": fact.fact_type,
                "value": fact.value,
                "evidence_start": fact.evidence_start,
                "evidence_end": fact.evidence_end,
                "evidence_quote": fact.evidence_quote,
                "feature_keys": fact.feature_keys,
                "critical": fact.critical,
            }
            for fact in draft.facts
        ],
        "provider": draft.provider,
        "model": draft.model,
        "warnings": draft.warnings,
        "needs_review": draft.needs_review,
    }


def _drafts_consistent(primary: ClinicalNoteDraft, review: ClinicalNoteDraft) -> bool:
    def signature(draft: ClinicalNoteDraft) -> set[tuple[str, str, str, bool]]:
        return {
            (
                fact.fact_type.strip().lower(),
                fact.value.strip().lower(),
                fact.evidence_quote,
                fact.critical,
            )
            for fact in draft.facts
        }

    primary_summary = " ".join(primary.summary.lower().split())
    review_summary = " ".join(review.summary.lower().split())
    return signature(primary) == signature(review) and primary_summary == review_summary


def _source_for_job(
    session: Session, context: RequestContext, job: Job, payload: dict[str, Any]
) -> tuple[EntryVersion, str]:
    try:
        version_id = uuid.UUID(str(payload["source_entry_version_id"]))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid source version")
    version = session.exec(
        select(EntryVersion).where(
            EntryVersion.clinic_id == context.clinic_id,
            EntryVersion.id == version_id,
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Source version not found")
    entry = session.exec(
        select(Entry).where(
            Entry.clinic_id == context.clinic_id,
            Entry.id == version.entry_id,
            Entry.patient_id == job.patient_id,
        )
    ).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Source version not found")
    _, content = decrypt_version(version)
    return version, content


def _create_ai_entry(
    session: Session,
    context: RequestContext,
    job: Job,
    *,
    summary: str,
    facts: list[ClinicalFact],
    interaction_type: str,
) -> tuple[Entry, EntryVersion]:
    entry_type_by_interaction = {
        "doctor_consult": "ai_doctor_consult_summary",
        "care_note": "ai_nurse_consult_summary",
        "patient_insight": "ai_patient_session_summary",
        "voice_session": "ai_patient_session_summary",
    }
    entry = Entry(
        clinic_id=context.clinic_id,
        patient_id=job.patient_id,
        section="system",
        origin="ai",
        entry_type=entry_type_by_interaction.get(
            interaction_type, "ai_doctor_consult_summary"
        ),
        patient_facing=False,
        source_job_id=job.id,
    )
    session.add(entry)
    session.flush()
    content = json.dumps(
        {
            "summary": summary,
            "facts": [
                {"fact_type": item.fact_type, "value": item.value} for item in facts
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    version_id = uuid.uuid4()
    version = EntryVersion(
        id=version_id,
        clinic_id=context.clinic_id,
        entry_id=entry.id,
        version_no=1,
        title_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "entry_version.title",
            version_id,
            f"AI {interaction_type}",
        ),
        content_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "entry_version.content", version_id, content
        ),
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        patient_facing=False,
        author_id=context.user_id,
    )
    session.add(version)
    session.flush()
    entry.current_version_id = version.id
    session.add(entry)
    return entry, version


def _create_fact_provenance(
    session: Session,
    context: RequestContext,
    *,
    job: Job,
    source_version: EntryVersion,
    source_text: str,
    facts: list[ClinicalFact],
    needs_review: bool,
    rule_derived: bool,
    provider: str,
    model: str,
) -> list[Highlight]:
    persisted: list[Highlight] = []
    for fact in facts:
        quote = source_text[fact.evidence_start : fact.evidence_end]
        if not quote or quote != fact.evidence_quote:
            continue
        normalized = _normalized_candidate_fact(fact, quote)
        candidate_fingerprint = _candidate_fingerprint(
            source_version.id,
            fact,
            quote,
            normalized=normalized,
        )
        existing = session.exec(
            select(Highlight)
            .where(
                Highlight.clinic_id == context.clinic_id,
                Highlight.candidate_fingerprint == candidate_fingerprint,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if existing is not None:
            _reuse_ai_candidate(
                session,
                context,
                existing,
                source_version=source_version,
                needs_review=needs_review,
            )
            persisted.append(existing)
            continue
        feature_keys = sanitize_feature_keys(fact.feature_keys)
        risk = deterministic_risk(
            fact_type=fact.fact_type,
            text=quote,
            model_risk="critical" if fact.critical else None,
        )
        effective_critical = risk.effective_risk == "critical"
        if effective_critical and "risk:critical" not in feature_keys:
            feature_keys.append("risk:critical")
        highlight_id = uuid.uuid4()
        highlight = Highlight(
            id=highlight_id,
            clinic_id=context.clinic_id,
            patient_id=job.patient_id,
            entry_id=source_version.entry_id,
            source_entry_version_id=source_version.id,
            candidate_fingerprint=candidate_fingerprint,
            label_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "highlight.label", highlight_id, fact.value
            ),
            status="pending",
            critical=effective_critical,
            patient_facing=False,
            anchor_state="resolved",
            # Evidence validity is independent from the AI run's clinical
            # review state. A clinician can accept a resolved fallback fact.
            review_required=False,
            feature_keys_json=feature_keys,
            unresolved=needs_review,
            created_by_id=context.user_id,
        )
        session.add(highlight)
        session.flush()
        refresh_highlight_score(session, highlight)
        pointer_id = uuid.uuid4()
        prefix = source_text[max(0, fact.evidence_start - 32) : fact.evidence_start]
        suffix = source_text[fact.evidence_end : fact.evidence_end + 32]
        pointer = ProvenancePointer(
            id=pointer_id,
            clinic_id=context.clinic_id,
            highlight_id=highlight.id,
            entry_version_id=source_version.id,
            start_offset=fact.evidence_start,
            end_offset=fact.evidence_end,
            exact_quote_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "provenance.exact_quote", pointer_id, quote
            ),
            prefix_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "provenance.prefix", pointer_id, prefix
            ),
            suffix_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "provenance.suffix", pointer_id, suffix
            ),
            quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        )
        session.add(pointer)
        session.flush()
        assertion = create_assertion(
            session,
            clinic_id=context.clinic_id,
            patient_id=job.patient_id,
            entry_id=source_version.entry_id,
            source_entry_version_id=source_version.id,
            provenance_pointer=pointer,
            fact_type=fact.fact_type,
            subject=normalized.key if normalized else fact.value,
            normalized_value=normalized.value if normalized else fact.value,
            origin="ai",
            highlight_id=highlight.id,
            polarity=normalized.polarity if normalized else "present",
            assertion_scope=(
                normalized.assertion_scope if normalized else "specific_substance"
            ),
            source_language=normalized.source_language if normalized else "und",
            clinical_status=(
                "review_required"
                if normalized is not None and normalized.review_required
                else "active"
            ),
        )
        from app.services.conflicts import detect_conflicts_for_assertion

        assertion_conflicts = detect_conflicts_for_assertion(
            session, context, assertion
        )
        if assertion_conflicts:
            highlight.unresolved = True
            session.add(highlight)
            refresh_highlight_score(session, highlight)
        request_parameters: dict[str, object] = {
            "schema": "clinical-fact-v2",
            "prompt": "fact-extraction-v2",
        }
        report = matching_calibration_report(
            session,
            clinic_id=context.clinic_id,
            provider=provider,
            exact_model_id=model,
            task="clinical_fact_extraction",
            request_parameters=request_parameters,
            dataset_manifest_sha256=evaluation_manifest_sha256(),
            code_commit=settings.NIGHTINGALE_SOURCE_COMMIT,
        )
        confidence_band = report.confidence_band if report else "unavailable"
        abstained = needs_review or confidence_band not in {"high", "medium"}
        session.add(
            DecisionAssessment(
                clinic_id=context.clinic_id,
                highlight_id=highlight.id,
                assertion_id=assertion.id,
                output_type=(
                    "rule_derived_suggestion" if rule_derived else "extracted_fact"
                ),
                support_state="supported",
                risk_tier=risk.effective_risk,
                deterministic_floor=risk.deterministic_floor,
                model_risk=risk.model_risk,
                effective_risk=risk.effective_risk,
                risk_rule_ids_json=risk.rule_ids,
                confidence_value=(report.accuracy_lower_bound if report else None),
                confidence_lower_bound=(
                    report.accuracy_lower_bound if report else None
                ),
                confidence_band=confidence_band,
                calibration_version=(str(report.id) if report else None),
                calibration_report_id=(report.id if report else None),
                abstained=abstained,
                abstention_reason=(
                    "clinical_review_required"
                    if needs_review
                    else "calibration_unavailable"
                    if report is None
                    else "calibrated_accuracy_below_threshold"
                    if confidence_band == "low"
                    else None
                ),
            )
        )
        persisted.append(highlight)
    return persisted


def _candidate_fingerprint(
    source_entry_version_id: uuid.UUID,
    fact: ClinicalFact,
    exact_quote: str,
    *,
    normalized: NormalizedFact | None = None,
) -> str:
    """Return one stable identity for a fact on an immutable source span.

    The digest intentionally excludes provider/model wording and job identity.
    A regeneration of the same fact therefore reuses the original highlight,
    assertion, provenance pointer, and any clinician-authored state attached to
    them. Task identity and normalized clinical semantics distinguish multiple
    assertions that share one source span, while the immutable source version,
    offsets, and quote digest keep the evidence addressable.
    """

    normalized = normalized or _normalized_candidate_fact(fact, exact_quote)
    fallback_value = _normalize_candidate_component(fact.value)
    payload = {
        "schema": "ai-candidate-v1",
        "task": "clinical_fact_extraction",
        "source_entry_version_id": str(source_entry_version_id),
        "fact_type": fact.fact_type.strip().casefold(),
        "entity": (
            _normalize_candidate_component(normalized.key)
            if normalized is not None
            else fallback_value
        ),
        "normalized_value": (
            _normalize_candidate_component(normalized.value)
            if normalized is not None
            else fallback_value
        ),
        "assertion_scope": (
            normalized.assertion_scope
            if normalized is not None
            else "specific_substance"
        ),
        "polarity": normalized.polarity if normalized is not None else "present",
        "evidence_start": fact.evidence_start,
        "evidence_end": fact.evidence_end,
        "quote_sha256": hashlib.sha256(exact_quote.encode()).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalize_candidate_component(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _normalized_candidate_fact(
    fact: ClinicalFact,
    exact_quote: str,
) -> NormalizedFact | None:
    """Resolve the semantic assertion represented by an extracted candidate.

    Providers sometimes return the complete evidence phrase as ``value`` and
    sometimes only the entity/value. Prefer a qualified parse of the provider
    value, then match a quote parse by its normalized entity. This keeps
    equivalent regenerations stable without collapsing two assertions that
    happen to share the same quote and offsets.
    """

    fact_type = fact.fact_type.strip().casefold()
    value_key = _normalize_candidate_component(fact.value)
    value_matches = [
        item
        for item in extract_normalized_facts(fact.value)
        if item.fact_type == fact_type
    ]
    if len(value_matches) == 1:
        return value_matches[0]
    if value_matches:
        exact_value_matches = [
            item
            for item in value_matches
            if _normalize_candidate_component(item.key) == value_key
            or _normalize_candidate_component(item.value) == value_key
        ]
        if len(exact_value_matches) == 1:
            return exact_value_matches[0]

    quote_matches = [
        item
        for item in extract_normalized_facts(exact_quote)
        if item.fact_type == fact_type
    ]
    entity_matches = [
        item
        for item in quote_matches
        if _normalize_candidate_component(item.key) in value_key
        or value_key in _normalize_candidate_component(item.key)
    ]
    if len(entity_matches) == 1:
        return entity_matches[0]
    if len(quote_matches) == 1 and value_key == _normalize_candidate_component(
        exact_quote
    ):
        return quote_matches[0]
    if len(quote_matches) == 1 and fact_type in {"dose", "route", "frequency"}:
        return quote_matches[0]
    return None


def _reuse_ai_candidate(
    session: Session,
    context: RequestContext,
    highlight: Highlight,
    *,
    source_version: EntryVersion,
    needs_review: bool,
) -> None:
    """Revalidate a persisted candidate without rewriting its human state."""

    if (
        highlight.entry_id != source_version.entry_id
        or highlight.source_entry_version_id != source_version.id
    ):
        raise _InternalJobError("AI_CANDIDATE_FINGERPRINT_COLLISION")
    pointer = session.exec(
        select(ProvenancePointer).where(
            ProvenancePointer.clinic_id == context.clinic_id,
            ProvenancePointer.highlight_id == highlight.id,
            ProvenancePointer.entry_version_id == source_version.id,
        )
    ).first()
    assertion = session.exec(
        select(ClinicalFactAssertion).where(
            ClinicalFactAssertion.clinic_id == context.clinic_id,
            ClinicalFactAssertion.highlight_id == highlight.id,
            ClinicalFactAssertion.source_entry_version_id == source_version.id,
            ClinicalFactAssertion.assertion_state == "active",
        )
    ).first()
    if pointer is None or assertion is None:
        raise _InternalJobError("AI_CANDIDATE_PROVENANCE_INCOMPLETE")
    # A reused candidate never re-enters the assessment branch, and the
    # (clinic, highlight) uniqueness means it can never acquire one later, so a
    # missing assessment here would be permanent.
    reused_assessment = session.exec(
        select(DecisionAssessment).where(
            DecisionAssessment.clinic_id == context.clinic_id,
            DecisionAssessment.highlight_id == highlight.id,
        )
    ).first()
    if reused_assessment is None:
        raise _InternalJobError("AI_CANDIDATE_ASSESSMENT_MISSING")

    from app.services.conflicts import detect_conflicts_for_assertion

    conflicts = detect_conflicts_for_assertion(session, context, assertion)
    # Never clear review/protection or overwrite accepted, pinned, confirmed,
    # learned, or provenance state during regeneration. A new failure/conflict
    # may only make the mutable safety projection more conservative.
    if needs_review or conflicts:
        highlight.unresolved = True
        session.add(highlight)
        refresh_highlight_score(session, highlight)


class _JobClaimLost(Exception):
    pass


def active_worker_for_context(
    session: Session, context: RequestContext, *, lock: bool = False
) -> tuple[ClinicMembership, User] | None:
    """Reload and validate the complete trusted worker identity from the database."""

    membership_statement = (
        select(ClinicMembership)
        .where(
            ClinicMembership.id == context.membership.id,
            ClinicMembership.clinic_id == context.clinic_id,
            ClinicMembership.user_id == context.user_id,
            ClinicMembership.role == "worker",
            col(ClinicMembership.is_active).is_(True),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        membership_statement = membership_statement.with_for_update(read=True)
    membership = session.exec(membership_statement).first()
    if membership is None:
        return None
    user_statement = (
        select(User)
        .where(User.id == membership.user_id, col(User.is_active).is_(True))
        .execution_options(populate_existing=True)
    )
    if lock:
        user_statement = user_statement.with_for_update(read=True)
    user = session.exec(user_statement).first()
    return (membership, user) if user is not None else None


def claim_job(
    session: Session,
    context: RequestContext,
    job_id: uuid.UUID,
    *,
    claim_token: uuid.UUID | None = None,
) -> Job:
    """Atomically claim one clinic/job-bound lease with SKIP LOCKED."""

    if (
        context.role != "worker"
        or context.job_id != job_id
        or context.membership.clinic_id != context.clinic_id
    ):
        raise HTTPException(status_code=403, detail="Worker job binding required")
    set_rls_clinic(session, context.clinic_id)
    if active_worker_for_context(session, context, lock=True) is None:
        raise HTTPException(status_code=403, detail="Active worker required")
    now = get_datetime_utc()
    unlocked = col(Job.locked_until).is_(None) | (col(Job.locked_until) < now)
    # ``pending`` is the explicit submit/manual-retry state. A failed row is
    # automatically claimable only when the failure path persisted a due retry
    # time (for example the bounded provider ladder). Permanent/malformed work
    # therefore cannot spin in the background merely because next_run_at is
    # NULL; a clinician must explicitly retry it.
    claimable = (
        ((col(Job.state) == "pending") & unlocked)
        | (
            (col(Job.state) == "failed")
            & unlocked
            & col(Job.next_run_at).is_not(None)
            & (col(Job.next_run_at) <= now)
        )
        | ((col(Job.state) == "running") & (col(Job.locked_until) < now))
    )
    job = session.exec(
        select(Job)
        .where(
            Job.clinic_id == context.clinic_id,
            Job.id == job_id,
            claimable,
        )
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).first()
    if job is None:
        raise HTTPException(status_code=409, detail={"code": "JOB_NOT_CLAIMABLE"})
    if job.attempt_count >= job.max_attempts:
        raise HTTPException(status_code=409, detail={"code": "JOB_ATTEMPTS_EXHAUSTED"})
    if job.state == "running":
        expired_attempts = session.exec(
            select(JobAttempt)
            .where(
                JobAttempt.clinic_id == context.clinic_id,
                JobAttempt.job_id == job.id,
                JobAttempt.status == "started",
            )
            .with_for_update()
        ).all()
        for expired_attempt in expired_attempts:
            expired_attempt.status = "failed"
            expired_attempt.error_code = "WORKER_LEASE_EXPIRED"
            expired_attempt.completed_at = now
            session.add(expired_attempt)
    job.state = "running"
    job.locked_by = str(claim_token or uuid.uuid4())
    lease_seconds = settings.AI_JOB_LEASE_SECONDS
    if job.kind in {"voice_process", "voice_reanalyze"}:
        lease_seconds = max(lease_seconds, settings.VOICE_JOB_LEASE_SECONDS)
    job.locked_until = now + timedelta(seconds=max(30, lease_seconds))
    job.error_code = None
    job.last_attempt_at = now
    job.updated_at = now
    session.add(job)
    session.flush()
    return job


def _lock_current_claim(
    session: Session,
    context: RequestContext,
    job_id: uuid.UUID,
    claim_token: uuid.UUID,
) -> tuple[Job, JobAttempt]:
    """Fence finalization against lease expiry, reclaim, or worker revocation."""

    set_rls_clinic(session, context.clinic_id)
    job = session.exec(
        select(Job)
        .where(Job.clinic_id == context.clinic_id, Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    attempt = session.exec(
        select(JobAttempt)
        .where(
            JobAttempt.clinic_id == context.clinic_id,
            JobAttempt.id == claim_token,
            JobAttempt.job_id == job_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    now = get_datetime_utc()
    if (
        job is None
        or attempt is None
        or job.state != "running"
        or job.locked_by != str(claim_token)
        or job.locked_until is None
        or job.locked_until <= now
        or attempt.status != "started"
        or attempt.worker_membership_id != context.membership.id
        or active_worker_for_context(session, context, lock=True) is None
    ):
        raise _JobClaimLost
    return job, attempt


async def process_job(
    session: Session, context: RequestContext, job_id: uuid.UUID
) -> Job:
    claim_token = uuid.uuid4()
    job = claim_job(session, context, job_id, claim_token=claim_token)
    if (
        session.exec(
            select(AIRun).where(
                AIRun.clinic_id == context.clinic_id, AIRun.job_id == job.id
            )
        ).first()
        is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "JOB_ALREADY_COMPLETED"})

    attempt = JobAttempt(
        id=claim_token,
        clinic_id=context.clinic_id,
        job_id=job.id,
        worker_membership_id=context.membership.id,
        attempt_no=job.attempt_count + 1,
    )
    job.attempt_count += 1
    session.add(job)
    session.add(attempt)
    session.flush()
    # Persist the lease before any external provider work. This releases the
    # row lock while keeping a durable recovery boundary for another worker.
    session.commit()
    attempt_work = session.begin_nested()
    text_deadline = _TextJobDeadline(settings.AI_TEXT_JOB_TIMEOUT_SECONDS)
    try:
        payload = field_codec.decrypt_json(
            context.clinic_id, "job.payload", job.id, job.payload_ciphertext
        )
        if not isinstance(payload, dict):
            raise _InternalJobError("INVALID_JOB_PAYLOAD")
        source_version, source_text = _source_for_job(session, context, job, payload)
        interaction_type = _trusted_interaction_type(payload)
        high_risk, conflict_review = _server_risk_flags(
            session, context, job.patient_id, source_version, source_text
        )
        extraction_context = ExtractionContext(
            clinic_id=context.clinic_id,
            patient_id=job.patient_id,
            source_version_id=source_version.id,
            interaction_type=interaction_type,
            high_risk=high_risk,
            conflict_review=conflict_review,
        )
        fallback = DeterministicClinicalNoteProvider(review_model_configured=False)
        pipeline = ClinicalScribePipeline(
            RedactionService(
                require_presidio=settings.PRESIDIO_REQUIRED,
                presidio_model=settings.PRESIDIO_NLP_MODEL,
            ),
            fallback_provider=fallback,
        )
        redaction_qualified = redaction_is_qualified(
            session, clinic_id=context.clinic_id
        )
        remote_provider = (
            _configured_remote_provider(session, context.clinic_id)
            if redaction_qualified
            else None
        )
        known_names = _trusted_patient_names(session, context, job.patient_id)
        provider_failure: ProviderFailure | None = None
        remote_succeeded = False
        if remote_provider is not None:
            try:
                _assert_provider_circuit_available(session, context.clinic_id)
                # The Responses endpoint is non-streaming in this adapter, so
                # completion of extract() is its first usable result boundary.
                result = await text_deadline.wait_for(
                    pipeline.run(
                        source_text,
                        context=extraction_context,
                        known_names=known_names,
                        remote_provider=remote_provider,
                    ),
                    stage_timeout_seconds=(
                        settings.REMOTE_FIRST_RESULT_TIMEOUT_SECONDS
                    ),
                )
                remote_succeeded = True
            except Exception as exc:
                provider_failure = classify_provider_failure(exc)
                if job.kind == "ai_recovery":
                    raise
                # Preserve manual work during an outage with a local,
                # rule-derived suggestion. It is always review-only and the
                # remote recovery is scheduled independently below.
                result = await text_deadline.wait_for(
                    pipeline.run(
                        source_text,
                        context=extraction_context,
                        known_names=known_names,
                        remote_provider=None,
                    ),
                )
        else:
            result = await text_deadline.wait_for(
                pipeline.run(
                    source_text,
                    context=extraction_context,
                    known_names=known_names,
                    remote_provider=None,
                ),
            )
        high_risk = high_risk or any(fact.critical for fact in result.draft.facts)
        review_required = high_risk or conflict_review
        review_draft: ClinicalNoteDraft | None = None
        review_status = "not_required"
        review_model = getattr(remote_provider, "review_model", None)
        review_warnings: list[str] = []
        if review_required:
            if remote_provider is None or result.used_fallback or not review_model:
                review_status = "unavailable"
                review_warnings.append("HIGH_RISK_REVIEW_MODEL_UNAVAILABLE")
            else:
                try:
                    review_context = replace(
                        extraction_context,
                        high_risk=high_risk,
                        conflict_review=conflict_review,
                    )
                    review_draft = validate_evidence(
                        await text_deadline.wait_for(
                            TextModelEgressGateway(remote_provider).review(
                                result.redaction,
                                review_context,
                                result.draft,
                            ),
                            stage_timeout_seconds=(
                                settings.REMOTE_REQUEST_TIMEOUT_SECONDS
                            ),
                        ),
                        result.redaction.redacted_text,
                    )
                    review_status = (
                        "consistent"
                        if _drafts_consistent(result.draft, review_draft)
                        else "disagreed"
                    )
                    review_warnings.extend(review_draft.warnings)
                except Exception as exc:
                    provider_failure = classify_provider_failure(exc)
                    review_status = "error"
                    review_warnings.append("HIGH_RISK_REVIEW_FAILED")

        # Synchronous mapping and persistence must not publish output after an
        # async stage consumed the complete whole-job budget.
        text_deadline.raise_if_expired()

        facts, raw_mapping_failed = _map_facts_to_source(
            result.draft.facts, source_text
        )
        warnings = _safe_warning_codes([*result.draft.warnings, *review_warnings])
        if not redaction_qualified:
            warnings.append("REDACTION_EVALUATION_REQUIRED")
        if raw_mapping_failed:
            warnings.append("INVALID_RAW_EVIDENCE_SPAN")
        needs_review = (
            result.draft.needs_review
            or raw_mapping_failed
            or result.used_fallback
            or provider_failure is not None
            or (review_required and review_status != "consistent")
            or bool(review_draft and review_draft.needs_review)
        )

        # External work ran after the durable lease commit. Re-lock and verify
        # the unique attempt token before any derived row is written.
        job, attempt = _lock_current_claim(session, context, job_id, claim_token)

        redaction_run = RedactionRun(
            clinic_id=context.clinic_id,
            source_entry_version_id=source_version.id,
            status=result.redaction.status,
            input_sha256=result.redaction.input_sha256,
            redacted_sha256=result.redaction.redacted_sha256,
            entity_counts_json=result.redaction.entity_counts,
            map_ciphertext=result.redaction.map_ciphertext,
            residual_scan_passed=result.redaction.residual_scan_passed,
            error_code=result.redaction.error_code,
        )
        session.add(redaction_run)
        session.flush()
        output_entry, output_version = _create_ai_entry(
            session,
            context,
            job,
            summary=result.draft.summary,
            facts=facts,
            interaction_type=interaction_type,
        )
        _create_fact_provenance(
            session,
            context,
            job=job,
            source_version=source_version,
            source_text=source_text,
            facts=facts,
            needs_review=needs_review,
            rule_derived=result.used_fallback,
            provider=result.draft.provider,
            model=result.draft.model,
        )
        fallback_reason: str | None = None
        if result.used_fallback:
            if provider_failure is not None:
                fallback_reason = provider_failure.code
            elif not redaction_qualified:
                fallback_reason = "REDACTION_EVALUATION_REQUIRED"
            elif result.redaction.error_code:
                fallback_reason = result.redaction.error_code
            elif settings.AI_PROVIDER == "disabled":
                fallback_reason = "PROVIDER_DISABLED"
            elif settings.AI_PROVIDER == "openai":
                fallback_reason = "REMOTE_PROVIDER_NOT_CONFIGURED"
            else:
                fallback_reason = "DETERMINISTIC_MODE"
        run_id = uuid.uuid4()
        run = AIRun(
            id=run_id,
            clinic_id=context.clinic_id,
            patient_id=job.patient_id,
            job_id=job.id,
            redaction_run_id=redaction_run.id,
            source_entry_version_id=source_version.id,
            executed_by_worker_membership_id=context.membership.id,
            interaction_type=interaction_type,
            provider=result.draft.provider,
            model=result.draft.model,
            review_model=review_model,
            review_status=review_status,
            primary_output_ciphertext=field_codec.encrypt_json(
                context.clinic_id,
                "ai_run.primary_output",
                run_id,
                _draft_payload(result.draft),
            ),
            review_output_ciphertext=(
                field_codec.encrypt_json(
                    context.clinic_id,
                    "ai_run.review_output",
                    run_id,
                    _draft_payload(review_draft),
                )
                if review_draft is not None
                else None
            ),
            status="fallback" if result.used_fallback else "completed",
            risk_tier="high" if review_required else "standard",
            fallback_reason=fallback_reason,
            needs_review=needs_review,
            request_sha256=job.request_sha256,
            output_entry_id=output_entry.id,
            output_entry_version_id=output_version.id,
            warnings_json=sorted(set(warnings)),
        )
        session.add(run)
        # Make both remote output and outage rule suggestions visible without
        # waiting for a later, unrelated domain event. The review queue remains
        # independent from the current-priority top-five cap.
        session.flush()
        rebuild_glance(session, context, job.patient_id)
        attempt.status = "completed"
        if provider_failure is not None:
            attempt.error_code = provider_failure.code
            attempt.error_class = provider_failure.failure_class
        attempt.completed_at = get_datetime_utc()
        attempt.duration_ms = max(
            0,
            int((attempt.completed_at - attempt.started_at).total_seconds() * 1_000),
        )
        job.state = "needs_review" if needs_review else "completed"
        job.locked_by = None
        job.locked_until = None
        job.updated_at = get_datetime_utc()
        recovery_job: Job | None = None
        if provider_failure is not None:
            next_retry_at = _record_provider_failure(
                session, job, provider_failure, retry_index=1
            )
            attempt.retry_scheduled_at = next_retry_at
            if provider_failure.retryable and next_retry_at is not None:
                recovery_payload = dict(payload)
                recovery_payload["recovery_of_job_id"] = str(job.id)
                recovery_job, _ = create_or_replay_job(
                    session,
                    context,
                    patient_id=job.patient_id,
                    kind="ai_recovery",
                    idempotency_key=f"provider-recovery:{job.id}",
                    payload=recovery_payload,
                )
                recovery_job.state = "failed"
                recovery_job.max_attempts = 5
                recovery_job.next_run_at = next_retry_at
                recovery_job.error_code = provider_failure.code
                recovery_job.error_class = provider_failure.failure_class
                recovery_job.provider_outage = True
                recovery_job.delayed_at = get_datetime_utc()
                recovery_job.retry_history_json = [
                    {
                        "attempt": 0,
                        "error_code": provider_failure.code,
                        "error_class": provider_failure.failure_class,
                        "attempted_at": job.last_attempt_at.isoformat()
                        if job.last_attempt_at
                        else get_datetime_utc().isoformat(),
                        "next_retry_at": next_retry_at.isoformat(),
                        "source_job_id": str(job.id),
                    }
                ]
                session.add(recovery_job)
                history = list(job.retry_history_json)
                if history:
                    history[-1] = {
                        **history[-1],
                        "recovery_job_id": str(recovery_job.id),
                    }
                    job.retry_history_json = history
                    session.add(job)
        elif remote_succeeded:
            _record_provider_success(session, job)
        session.add(attempt)
        session.add(job)
        emit_change(
            session,
            context,
            action="ai.completed",
            resource_type="ai_run",
            resource_id=run.id,
            metadata={
                "job_id": str(job.id),
                "state": job.state,
                "fallback": result.used_fallback,
                "review_status": review_status,
                "provider_outage": provider_failure is not None,
                "recovery_job_id": str(recovery_job.id) if recovery_job else None,
            },
        )
        session.flush()
        attempt_work.commit()
    except _JobClaimLost:
        attempt_work.rollback()
        session.rollback()
        raise HTTPException(status_code=409, detail={"code": "JOB_CLAIM_LOST"})
    except Exception as exc:
        attempt_work.rollback()
        session.rollback()
        try:
            job, attempt = _lock_current_claim(session, context, job_id, claim_token)
        except _JobClaimLost:
            session.rollback()
            raise HTTPException(status_code=409, detail={"code": "JOB_CLAIM_LOST"})
        failure = classify_provider_failure(exc)
        error_code = (
            exc.code
            if isinstance(exc, _InternalJobError)
            else failure.code
            if failure.code != "PROVIDER_FAILURE"
            else "AI_JOB_FAILED"
        )
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.error_class = failure.failure_class
        attempt.completed_at = get_datetime_utc()
        job.state = "failed"
        job.error_code = error_code
        job.error_class = failure.failure_class
        job.locked_by = None
        job.locked_until = None
        retry_at: datetime | None = None
        if job.kind == "ai_recovery" and failure.retryable:
            retry_at = _record_provider_failure(
                session,
                job,
                failure,
                # The parent job already used the first 30-second slot.
                retry_index=job.attempt_count + 1,
            )
            if job.attempt_count >= job.max_attempts:
                retry_at = None
                job.next_run_at = None
        attempt.retry_scheduled_at = retry_at
        attempt.duration_ms = max(
            0,
            int((attempt.completed_at - attempt.started_at).total_seconds() * 1_000),
        )
        job.updated_at = get_datetime_utc()
        session.add(attempt)
        session.add(job)
        emit_change(
            session,
            context,
            action="ai.failed",
            resource_type="job",
            resource_id=job.id,
            metadata={
                "error_code": error_code,
                "error_class": failure.failure_class,
                "next_retry_at": (retry_at.isoformat() if retry_at else None),
            },
        )
    session.flush()
    session.commit()
    return job
