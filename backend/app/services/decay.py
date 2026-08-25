from __future__ import annotations

import hashlib
import io
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException
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
    RetentionLock,
)
from app.services.nightingale import decrypt_version

POLICY_VERSION = "nightingale-decay-v1"
HOT_DAYS = 180
WARM_DAYS = 730
MAX_REHYDRATED_BYTES = 5_000_000


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
) -> bool:
    locks = session.exec(
        select(RetentionLock).where(
            RetentionLock.clinic_id == clinic_id,
            col(RetentionLock.entity_id).in_([entry.id, version.id]),
            col(RetentionLock.entity_type).in_(["entry", "entry_version"]),
        )
    ).all()
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
) -> list[str]:
    reasons: list[str] = []
    highlight = session.exec(
        select(Highlight).where(
            Highlight.clinic_id == context.clinic_id,
            Highlight.source_entry_version_id == version.id,
            (
                col(Highlight.critical).is_(True)
                | col(Highlight.unresolved).is_(True)
                | col(Highlight.pinned).is_(True)
                | col(Highlight.clinician_confirmed).is_(True)
            ),
        )
    ).first()
    if highlight is not None:
        if highlight.critical:
            reasons.append("critical")
        if highlight.unresolved:
            reasons.append("unresolved")
        if highlight.pinned:
            reasons.append("pinned")
        if highlight.clinician_confirmed:
            reasons.append("clinician_confirmed")
    conflict = session.exec(
        select(ConflictCase).where(
            ConflictCase.clinic_id == context.clinic_id,
            ConflictCase.status == "unresolved",
            (ConflictCase.left_entry_id == entry.id)
            | (ConflictCase.right_entry_id == entry.id),
        )
    ).first()
    if conflict is not None:
        reasons.append("unresolved_conflict")
    task = session.exec(
        select(CareTask).where(
            CareTask.clinic_id == context.clinic_id,
            CareTask.patient_id == entry.patient_id,
            CareTask.status != "completed",
        )
    ).first()
    if task is not None:
        reasons.append("open_task")
    if _active_retention_lock(session, context.clinic_id, entry, version, now):
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
        raise ValueError("ARCHIVE_EXPANSION_LIMIT")
    return output


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
            blob.clinic_id, "archive.payload", blob.id, blob.payload_ciphertext
        )
        payload = _zstd_decompress(compressed)
    except Exception:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ARCHIVE_INTEGRITY_ERROR",
                "message": "Archive authentication failed",
            },
        )
    if len(payload) > MAX_REHYDRATED_BYTES:
        raise HTTPException(status_code=413, detail="Archive expands beyond limit")
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
    candidates = {
        item.entry_version_id: item
        for item in list_decay_candidates(session, context, now=now)
    }
    candidate = candidates.get(version.id)
    if candidate is None or not candidate.eligible_for_cold:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DECAY_NOT_ELIGIBLE",
                "protected_reasons": candidate.protected_reasons if candidate else [],
            },
        )
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
    encrypted = field_codec.encrypt(
        context.clinic_id, "archive.payload", blob_id, compressed
    )
    blob = ArchiveBlob(
        id=blob_id,
        clinic_id=context.clinic_id,
        entry_version_id=version.id,
        payload_ciphertext=encrypted,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
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
