from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

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
    ClinicMembership,
    ConflictCase,
    Entry,
    EntryVersion,
    Highlight,
    InteractionType,
    Job,
    JobAttempt,
    JobPublic,
    Patient,
    ProvenancePointer,
    RedactionRun,
    User,
    get_datetime_utc,
)
from app.services.importance import refresh_highlight_score, sanitize_feature_keys
from app.services.nightingale import decrypt_version, emit_change, get_patient
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
        "PROVIDER_FACT_SCHEMA_INVALID",
        "PROVIDER_REPORTED_WARNING",
        "PROVIDER_WARNING_SCHEMA_INVALID",
        "REDACTION_REVIEW",
        "RESIDUAL_PHI_DETECTED",
    }
)


class _InternalJobError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


def _configured_remote_provider() -> OpenAITextProvider | None:
    if (
        settings.AI_PROVIDER != "openai"
        or not settings.REMOTE_TEXT_EGRESS_ENABLED
        or not settings.OPENAI_API_KEY
        or not settings.OPENAI_EXTRACT_MODEL
    ):
        return None
    return OpenAITextProvider(
        api_key=settings.OPENAI_API_KEY,
        extract_model=settings.OPENAI_EXTRACT_MODEL,
        review_model=settings.OPENAI_REVIEW_MODEL,
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


def job_public(session: Session, job: Job) -> JobPublic:
    run = session.exec(
        select(AIRun).where(AIRun.clinic_id == job.clinic_id, AIRun.job_id == job.id)
    ).first()
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
    )


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
    entry = Entry(
        clinic_id=context.clinic_id,
        patient_id=job.patient_id,
        section="system",
        origin="ai",
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
) -> None:
    for fact in facts:
        quote = source_text[fact.evidence_start : fact.evidence_end]
        if not quote or quote != fact.evidence_quote:
            continue
        feature_keys = sanitize_feature_keys(fact.feature_keys)
        if fact.critical and "risk:critical" not in feature_keys:
            feature_keys.append("risk:critical")
        highlight_id = uuid.uuid4()
        highlight = Highlight(
            id=highlight_id,
            clinic_id=context.clinic_id,
            patient_id=job.patient_id,
            entry_id=source_version.entry_id,
            source_entry_version_id=source_version.id,
            label_ciphertext=field_codec.encrypt_text(
                context.clinic_id, "highlight.label", highlight_id, fact.value
            ),
            status="pending",
            critical=fact.critical,
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
        session.add(
            ProvenancePointer(
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
        )


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
    claimable = (
        col(Job.state).in_(["pending", "failed"])
        & (col(Job.locked_until).is_(None) | (col(Job.locked_until) < now))
    ) | ((col(Job.state) == "running") & (col(Job.locked_until) < now))
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
        remote_provider = _configured_remote_provider()
        result = await pipeline.run(
            source_text,
            context=extraction_context,
            known_names=_trusted_patient_names(session, context, job.patient_id),
            remote_provider=remote_provider,
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
                        await remote_provider.review(
                            result.redaction.redacted_text,
                            review_context,
                            result.draft,
                        ),
                        result.redaction.redacted_text,
                    )
                    review_status = (
                        "consistent"
                        if _drafts_consistent(result.draft, review_draft)
                        else "disagreed"
                    )
                    review_warnings.extend(review_draft.warnings)
                except Exception:
                    review_status = "error"
                    review_warnings.append("HIGH_RISK_REVIEW_FAILED")

        facts, raw_mapping_failed = _map_facts_to_source(
            result.draft.facts, source_text
        )
        warnings = _safe_warning_codes([*result.draft.warnings, *review_warnings])
        if raw_mapping_failed:
            warnings.append("INVALID_RAW_EVIDENCE_SPAN")
        needs_review = (
            result.draft.needs_review
            or raw_mapping_failed
            or result.used_fallback
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
        )
        fallback_reason: str | None = None
        if result.used_fallback:
            if result.redaction.error_code:
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
        attempt.status = "completed"
        attempt.completed_at = get_datetime_utc()
        job.state = "needs_review" if needs_review else "completed"
        job.locked_by = None
        job.locked_until = None
        job.updated_at = get_datetime_utc()
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
        error_code = exc.code if isinstance(exc, _InternalJobError) else "AI_JOB_FAILED"
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.completed_at = get_datetime_utc()
        job.state = "failed"
        job.error_code = error_code
        job.locked_by = None
        job.locked_until = None
        job.updated_at = get_datetime_utc()
        session.add(attempt)
        session.add(job)
        emit_change(
            session,
            context,
            action="ai.failed",
            resource_type="job",
            resource_id=job.id,
            metadata={"error_code": error_code},
        )
    session.flush()
    session.commit()
    return job
