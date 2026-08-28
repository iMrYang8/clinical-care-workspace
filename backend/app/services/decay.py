from __future__ import annotations

import hashlib
import io
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    ArchiveBlob,
    CareTask,
    ConflictCase,
    Entry,
    EntryVersion,
    Highlight,
    Patient,
    PatientPublication,
    PatientSharingRequest,
    RetentionLock,
)
from app.services.nightingale import decrypt_version

POLICY_VERSION = "nightingale-decay-v1"
HOT_DAYS = 180
WARM_DAYS = 730
MAX_REHYDRATED_BYTES = 5_000_000


class ArchiveExpansionError(ValueError):
    pass


@dataclass(frozen=True)
class DecayCandidate:
    entry_version_id: uuid.UUID
    entry_id: uuid.UUID
    storage_tier: str
    age_days: int
    eligible_for_cold: bool
    protected_reasons: list[str]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _active_retention_lock(
    session: Session,
    clinic_id: uuid.UUID,
    entry: Entry,
    version: EntryVersion,
    now: datetime,
    *,
    lock: bool = False,
) -> bool:
    statement = select(RetentionLock).where(
        RetentionLock.clinic_id == clinic_id,
        col(RetentionLock.entity_id).in_([entry.id, version.id]),
        col(RetentionLock.entity_type).in_(["entry", "entry_version"]),
    )
    if lock:
        statement = statement.with_for_update()
    locks = session.exec(statement).all()
    return any(
        lock.locked_until is None or _aware(lock.locked_until) > now for lock in locks
    )


def protected_reasons(
    session: Session,
    context: RequestContext,
    entry: Entry,
    version: EntryVersion,
    *,
    now: datetime,
    lock: bool = False,
) -> list[str]:
    reasons: list[str] = []
    highlight_statement = select(Highlight).where(
        Highlight.clinic_id == context.clinic_id,
        Highlight.source_entry_version_id == version.id,
    )
    if not lock:
        highlight_statement = highlight_statement.where(
            col(Highlight.critical).is_(True)
            | col(Highlight.unresolved).is_(True)
            | col(Highlight.pinned).is_(True)
            | col(Highlight.clinician_confirmed).is_(True)
        )
    if lock:
        highlight_statement = highlight_statement.with_for_update()
    highlights = session.exec(highlight_statement).all()
    for highlight in highlights:
        if highlight.critical:
            reasons.append("critical")
        if highlight.unresolved:
            reasons.append("unresolved")
        if highlight.pinned:
            reasons.append("pinned")
        if highlight.clinician_confirmed:
            reasons.append("clinician_confirmed")
    conflict_statement = select(ConflictCase).where(
        ConflictCase.clinic_id == context.clinic_id,
        (ConflictCase.left_entry_id == entry.id)
        | (ConflictCase.right_entry_id == entry.id),
    )
    if not lock:
        conflict_statement = conflict_statement.where(
            ConflictCase.status == "unresolved"
        )
    if lock:
        conflict_statement = conflict_statement.with_for_update()
    conflicts = session.exec(conflict_statement).all()
    if any(conflict.status == "unresolved" for conflict in conflicts):
        reasons.append("unresolved_conflict")
    task_statement = select(CareTask).where(
        CareTask.clinic_id == context.clinic_id,
        CareTask.patient_id == entry.patient_id,
    )
    if not lock:
        task_statement = task_statement.where(CareTask.status != "completed")
    if lock:
        task_statement = task_statement.with_for_update()
    tasks = session.exec(task_statement).all()
    if any(task.status != "completed" for task in tasks):
        reasons.append("open_task")
    sharing_statement = select(PatientSharingRequest).where(
        PatientSharingRequest.clinic_id == context.clinic_id,
        PatientSharingRequest.entry_version_id == version.id,
        PatientSharingRequest.status == "pending",
    )
    if lock:
        sharing_statement = sharing_statement.with_for_update()
    if session.exec(sharing_statement).first() is not None:
        reasons.append("pending_patient_sharing")
    publication_statement = select(PatientPublication).where(
        PatientPublication.clinic_id == context.clinic_id,
        PatientPublication.entry_version_id == version.id,
        col(PatientPublication.withdrawn_at).is_(None),
    )
    if lock:
        publication_statement = publication_statement.with_for_update()
    if session.exec(publication_statement).first() is not None:
        reasons.append("active_patient_publication")
    if _active_retention_lock(
        session, context.clinic_id, entry, version, now, lock=lock
    ):
        reasons.append("retention_lock")
    return sorted(set(reasons))


def list_decay_candidates(
    session: Session,
    context: RequestContext,
    *,
    now: datetime | None = None,
) -> list[DecayCandidate]:
    effective_now = _aware(now or datetime.now(UTC))
    versions = session.exec(
        select(EntryVersion).where(EntryVersion.clinic_id == context.clinic_id)
    ).all()
    output: list[DecayCandidate] = []
    for version in versions:
        entry = session.exec(
            select(Entry).where(
                Entry.clinic_id == context.clinic_id, Entry.id == version.entry_id
            )
        ).first()
        if entry is None:
            continue
        age_days = max(0, (effective_now - _aware(version.created_at)).days)
        policy_tier = (
            "hot"
            if age_days <= HOT_DAYS
            else "warm"
            if age_days <= WARM_DAYS
            else "cold"
        )
        reasons = protected_reasons(session, context, entry, version, now=effective_now)
        eligible = (
            age_days > WARM_DAYS
            and version.storage_tier != "cold"
            and not reasons
            and version.content_ciphertext is not None
            and version.title_ciphertext is not None
        )
        output.append(
            DecayCandidate(
                entry_version_id=version.id,
                entry_id=entry.id,
                storage_tier=version.storage_tier
                if version.storage_tier == "cold"
                else "hot"
                if reasons
                else policy_tier,
                age_days=age_days,
                eligible_for_cold=eligible,
                protected_reasons=reasons,
            )
        )
    output.sort(key=lambda item: (-item.age_days, str(item.entry_version_id)))
    return output


def canonical_payload(title: str, content: str) -> bytes:
    return json.dumps(
        {"title": title, "content": content},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _zstd_compress(payload: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor(level=9).compress(payload)


def _zstd_decompress(payload: bytes) -> bytes:
    import zstandard

    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(payload)) as reader:
        output = reader.read(MAX_REHYDRATED_BYTES + 1)
    if len(output) > MAX_REHYDRATED_BYTES:
        raise ArchiveExpansionError("ARCHIVE_EXPANSION_LIMIT")
    return output


def _archive_namespace(blob: ArchiveBlob) -> str:
    """Bind an archive envelope to its immutable source version and hash."""

    return f"archive.payload:{blob.entry_version_id}:{blob.plaintext_sha256}"


def decode_archive(blob: ArchiveBlob) -> tuple[str, str]:
    if hashlib.sha256(blob.payload_ciphertext).hexdigest() != blob.ciphertext_sha256:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ARCHIVE_INTEGRITY_ERROR",
                "message": "Ciphertext hash mismatch",
            },
        )
    try:
        compressed = field_codec.decrypt(
            blob.clinic_id,
            _archive_namespace(blob),
            blob.id,
            blob.payload_ciphertext,
        )
        payload = _zstd_decompress(compressed)
    except ArchiveExpansionError:
        raise HTTPException(status_code=413, detail="Archive expands beyond limit")
    except Exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ARCHIVE_INTEGRITY_ERROR",
                "message": "Archive authentication failed",
            },
        )
    if hashlib.sha256(payload).hexdigest() != blob.plaintext_sha256:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ARCHIVE_INTEGRITY_ERROR",
                "message": "Plaintext hash mismatch",
            },
        )
    try:
        parsed = cast(dict[str, Any], json.loads(payload))
        title = parsed["title"]
        content = parsed["content"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=409, detail={"code": "ARCHIVE_INTEGRITY_ERROR"})
    if not isinstance(title, str) or not isinstance(content, str):
        raise HTTPException(status_code=409, detail={"code": "ARCHIVE_INTEGRITY_ERROR"})
    return title, content


def archive_version(
    session: Session,
    context: RequestContext,
    version: EntryVersion,
    *,
    now: datetime | None = None,
) -> ArchiveBlob:
    effective_now = _aware(now or datetime.now(UTC))
    # Serialize the polymorphic legal-hold relation first. The DB trigger on
    # retention_locks takes the same lock for INSERT/UPDATE/DELETE.
    if session.get_bind().dialect.name == "postgresql":
        for entity_id in sorted((version.entry_id, version.id), key=str):
            session.connection().execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:subject, 0))"),
                {"subject": f"nightingale-decay:{entity_id}"},
            )
    locked_version = session.exec(
        select(EntryVersion)
        .where(
            EntryVersion.clinic_id == context.clinic_id,
            EntryVersion.id == version.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if locked_version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    entry = session.exec(
        select(Entry)
        .where(
            Entry.clinic_id == context.clinic_id,
            Entry.id == locked_version.entry_id,
        )
        .with_for_update()
    ).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    # New tasks and conflicts acquire FK key-share locks on these protected
    # subjects; existing protection rows are locked by protected_reasons().
    session.exec(
        select(Patient)
        .where(
            Patient.clinic_id == context.clinic_id,
            Patient.id == entry.patient_id,
        )
        .with_for_update()
    ).one()
    reasons = protected_reasons(
        session,
        context,
        entry,
        locked_version,
        now=effective_now,
        lock=True,
    )
    age_days = max(0, (effective_now - _aware(locked_version.created_at)).days)
    eligible = (
        age_days > WARM_DAYS
        and locked_version.storage_tier != "cold"
        and not reasons
        and locked_version.content_ciphertext is not None
        and locked_version.title_ciphertext is not None
    )
    if not eligible:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DECAY_NOT_ELIGIBLE",
                "protected_reasons": reasons,
            },
        )
    version = locked_version
    title, content = decrypt_version(version)
    plaintext = canonical_payload(title, content)
    if version.archive_blob_id is not None:
        existing = session.exec(
            select(ArchiveBlob).where(
                ArchiveBlob.clinic_id == context.clinic_id,
                ArchiveBlob.id == version.archive_blob_id,
                ArchiveBlob.entry_version_id == version.id,
            )
        ).first()
        if existing is None:
            raise HTTPException(status_code=409, detail={"code": "ARCHIVE_MISSING"})
        checked_title, checked_content = decode_archive(existing)
        if checked_title != title or checked_content != content:
            raise HTTPException(
                status_code=409, detail={"code": "ARCHIVE_INTEGRITY_ERROR"}
            )
        version.title_ciphertext = None
        version.content_ciphertext = None
        version.storage_tier = "cold"
        session.add(version)
        session.flush()
        return existing
    compressed = _zstd_compress(plaintext)
    blob_id = uuid.uuid4()
    plaintext_sha256 = hashlib.sha256(plaintext).hexdigest()
    namespace = f"archive.payload:{version.id}:{plaintext_sha256}"
    encrypted = field_codec.encrypt(context.clinic_id, namespace, blob_id, compressed)
    blob = ArchiveBlob(
        id=blob_id,
        clinic_id=context.clinic_id,
        entry_version_id=version.id,
        payload_ciphertext=encrypted,
        plaintext_sha256=plaintext_sha256,
        ciphertext_sha256=hashlib.sha256(encrypted).hexdigest(),
        original_size=len(plaintext),
        compressed_size=len(compressed),
    )
    session.add(blob)
    session.flush()
    checked_title, checked_content = decode_archive(blob)
    if (
        checked_title != title
        or checked_content != content
        or hashlib.sha256(checked_content.encode()).hexdigest()
        != version.content_sha256
    ):
        raise HTTPException(status_code=409, detail={"code": "ARCHIVE_INTEGRITY_ERROR"})
    version.title_ciphertext = None
    version.content_ciphertext = None
    version.storage_tier = "cold"
    version.archive_blob_id = blob.id
    session.add(version)
    session.flush()
    return blob


def rehydrate_version(
    session: Session, context: RequestContext, version: EntryVersion
) -> EntryVersion:
    if version.clinic_id != context.clinic_id:
        raise HTTPException(status_code=404, detail="Version not found")
    # Always reload under a row lock. This makes concurrent rehydrate calls
    # idempotent and serializes them with archive_version's physical transition.
    locked_version = session.exec(
        select(EntryVersion)
        .where(
            EntryVersion.clinic_id == context.clinic_id,
            EntryVersion.id == version.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if locked_version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    version = locked_version
    if version.storage_tier != "cold" or version.archive_blob_id is None:
        return version
    blob = session.exec(
        select(ArchiveBlob).where(
            ArchiveBlob.clinic_id == context.clinic_id,
            ArchiveBlob.id == version.archive_blob_id,
            ArchiveBlob.entry_version_id == version.id,
        )
    ).first()
    if blob is None:
        raise HTTPException(status_code=409, detail={"code": "ARCHIVE_MISSING"})
    title, content = decode_archive(blob)
    if hashlib.sha256(content.encode()).hexdigest() != version.content_sha256:
        raise HTTPException(status_code=409, detail={"code": "ARCHIVE_INTEGRITY_ERROR"})
    version.title_ciphertext = field_codec.encrypt_text(
        version.clinic_id, "entry_version.title", version.id, title
    )
    version.content_ciphertext = field_codec.encrypt_text(
        version.clinic_id, "entry_version.content", version.id, content
    )
    version.storage_tier = "warm"
    session.add(version)
    session.flush()
    return version


def lock_active_version_for_protection(
    session: Session,
    context: RequestContext,
    version_id: uuid.UUID,
    *,
    require_active: bool = True,
) -> EntryVersion:
    """Serialize a trust transition with decay and optionally require content."""

    version = session.exec(
        select(EntryVersion)
        .where(
            EntryVersion.clinic_id == context.clinic_id,
            EntryVersion.id == version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if require_active and (
        version.storage_tier == "cold"
        or version.title_ciphertext is None
        or version.content_ciphertext is None
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOURCE_REHYDRATION_REQUIRED",
                "message": "Rehydrate the immutable source before protecting it",
            },
        )
    return version
