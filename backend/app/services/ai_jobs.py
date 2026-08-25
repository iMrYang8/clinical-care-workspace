from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, select

from app.api.deps import RequestContext
from app.core.config import settings
from app.core.field_crypto import field_codec
from app.models import (
    AIRun,
    AIRunPublic,
    Entry,
    EntryVersion,
    Highlight,
    Job,
    JobAttempt,
    JobPublic,
    ProvenancePointer,
    RedactionRun,
    get_datetime_utc,
)
from app.services.importance import refresh_highlight_score, sanitize_feature_keys
from app.services.nightingale import decrypt_version, emit_change, get_patient
from app.services.providers.base import ClinicalFact, ExtractionContext
from app.services.providers.deterministic import DeterministicClinicalNoteProvider
from app.services.providers.openai_text import OpenAITextProvider
from app.services.redaction import ClinicalScribePipeline, RedactionService

_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


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


def get_scoped_job(session: Session, context: RequestContext, job_id: uuid.UUID) -> Job:
    job = session.exec(
        select(Job).where(Job.clinic_id == context.clinic_id, Job.id == job_id)
    ).first()
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
    session.add(job)
    session.flush()
    emit_change(
        session,
        context,
        action="job.created",
        resource_type="job",
        resource_id=job.id,
        metadata={"kind": kind, "state": "pending"},
    )
    return job, False


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


async def process_job(session: Session, context: RequestContext, job: Job) -> Job:
    if job.attempt_count >= job.max_attempts:
        raise HTTPException(status_code=409, detail={"code": "JOB_ATTEMPTS_EXHAUSTED"})
    if (
        session.exec(
            select(AIRun).where(
                AIRun.clinic_id == context.clinic_id, AIRun.job_id == job.id
            )
        ).first()
        is not None
    ):
        return job

    attempt = JobAttempt(
        clinic_id=context.clinic_id,
        job_id=job.id,
        attempt_no=job.attempt_count + 1,
    )
    job.attempt_count += 1
    job.state = "running"
    job.error_code = None
    job.updated_at = get_datetime_utc()
    session.add(job)
    session.add(attempt)
    session.flush()
    attempt_work = session.begin_nested()
    try:
        payload = field_codec.decrypt_json(
            context.clinic_id, "job.payload", job.id, job.payload_ciphertext
        )
        if not isinstance(payload, dict):
            raise ValueError("INVALID_JOB_PAYLOAD")
        source_version, source_text = _source_for_job(session, context, job, payload)
        interaction_type = str(payload.get("interaction_type", "care_note"))[:60]
        high_risk = bool(payload.get("high_risk", False))
        conflict_review = bool(payload.get("conflict_review", False))
        known_names = [
            str(item)
            for item in [
                *payload.get("known_names", []),
                *payload.get("known_aliases", []),
            ]
        ]
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
            known_names=known_names,
            remote_provider=remote_provider,
        )
        facts, raw_mapping_failed = _map_facts_to_source(
            result.draft.facts, source_text
        )
        warnings = [
            warning
            for warning in result.draft.warnings
            if _SAFE_ERROR_CODE.fullmatch(warning)
        ]
        if raw_mapping_failed:
            warnings.append("INVALID_RAW_EVIDENCE_SPAN")
        # Deterministic extraction is a truthful fallback, not a silent stand-in
        # for configured model review.
        needs_review = (
            result.draft.needs_review or raw_mapping_failed or result.used_fallback
        )

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
        run = AIRun(
            clinic_id=context.clinic_id,
            patient_id=job.patient_id,
            job_id=job.id,
            redaction_run_id=redaction_run.id,
            source_entry_version_id=source_version.id,
            interaction_type=interaction_type,
            provider=result.draft.provider,
            model=result.draft.model,
            status="fallback" if result.used_fallback else "completed",
            risk_tier="high" if high_risk or conflict_review else "standard",
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
            },
        )
        session.flush()
        attempt_work.commit()
    except HTTPException:
        attempt_work.rollback()
        raise
    except Exception as exc:
        attempt_work.rollback()
        candidate_code = str(exc)
        error_code = (
            candidate_code
            if _SAFE_ERROR_CODE.fullmatch(candidate_code)
            else "AI_JOB_FAILED"
        )
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.completed_at = get_datetime_utc()
        job.state = "failed"
        job.error_code = error_code
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
    return job
