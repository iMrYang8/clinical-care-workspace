import difflib
import hashlib
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy import desc
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    AuditEvent,
    DomainEvent,
    Entry,
    EntryCreate,
    EntryPublic,
    EntryRelation,
    EntryVersion,
    EntryVersionPublic,
    Highlight,
    Patient,
    PatientGlanceSnapshot,
    PatientPublic,
    PatientTimelineEntry,
    PatientUserLink,
    ProvenancePointer,
    get_datetime_utc,
)
from app.services.importance import record_feedback, refresh_highlight_score


class VersionConflictError(Exception):
    def __init__(self, current_version_id: uuid.UUID) -> None:
        self.current_version_id = current_version_id


def normalize_etag(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    return normalized.strip('"')


def emit_change(
    session: Session,
    context: RequestContext,
    *,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    metadata: Mapping[str, object] | None = None,
) -> None:
    safe_metadata = dict(metadata or {})
    session.add(
        AuditEvent(
            clinic_id=context.clinic_id,
            actor_id=context.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_json=safe_metadata,
        )
    )
    session.add(
        DomainEvent(
            clinic_id=context.clinic_id,
            event_type=action,
            aggregate_type=resource_type,
            aggregate_id=resource_id,
            actor_id=context.user_id,
            payload_json=safe_metadata,
        )
    )


def get_patient(
    session: Session, context: RequestContext, patient_id: uuid.UUID
) -> Patient:
    patient = session.exec(
        select(Patient).where(
            Patient.id == patient_id, Patient.clinic_id == context.clinic_id
        )
    ).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    if context.role == "patient":
        link = session.exec(
            select(PatientUserLink).where(
                PatientUserLink.clinic_id == context.clinic_id,
                PatientUserLink.patient_id == patient.id,
                PatientUserLink.user_id == context.user_id,
            )
        ).first()
        if link is None:
            raise HTTPException(status_code=404, detail="Patient not found")
    return patient


def list_patients(session: Session, context: RequestContext) -> list[PatientPublic]:
    statement = select(Patient).where(Patient.clinic_id == context.clinic_id)
    if context.role == "patient":
        statement = statement.join(PatientUserLink).where(
            PatientUserLink.clinic_id == context.clinic_id,
            PatientUserLink.user_id == context.user_id,
        )
    patients = session.exec(statement.order_by(col(Patient.created_at))).all()
    return [
        PatientPublic(
            id=patient.id,
            display_name=field_codec.decrypt_text(
                patient.clinic_id,
                "patient.display_name",
                patient.id,
                patient.display_name_ciphertext,
            ),
        )
        for patient in patients
    ]


def get_scoped_entry(
    session: Session,
    context: RequestContext,
    entry_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Entry:
    statement = select(Entry).where(
        Entry.id == entry_id, Entry.clinic_id == context.clinic_id
    )
    if lock:
        statement = statement.with_for_update()
    entry = session.exec(statement).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    get_patient(session, context, entry.patient_id)
    if context.role == "patient" and (
        not entry.patient_facing or entry.origin in {"ai", "system"}
    ):
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


def authorize_entry_create(
    context: RequestContext, data: EntryCreate
) -> tuple[str, bool]:
    role = context.role
    if role == "staff" and data.section == "staff":
        return "human", data.patient_facing
    if role == "clinician" and data.section == "clinician":
        return "human", data.patient_facing
    if role == "patient" and data.section == "patient":
        return "human", True
    if (
        role == "worker"
        and context.job_id is not None
        and data.section == "system"
        and data.origin in {"ai", "system"}
    ):
        return data.origin, False
    raise HTTPException(status_code=403, detail="Role cannot write this section")


def authorize_entry_write(context: RequestContext, entry: Entry) -> None:
    if entry.origin in {"ai", "system"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AI_ENTRY_IMMUTABLE",
                "message": "Create a correction entry",
            },
        )
    allowed = {
        "staff": "staff",
        "clinician": "clinician",
        "patient": "patient",
    }
    if allowed.get(context.role) != entry.section:
        raise HTTPException(status_code=403, detail="Role cannot edit this section")


def _new_version(
    *,
    entry: Entry,
    version_no: int,
    title: str,
    content: str,
    author_id: uuid.UUID,
    patient_facing: bool,
    reverted_from_version_id: uuid.UUID | None = None,
) -> EntryVersion:
    version_id = uuid.uuid4()
    return EntryVersion(
        id=version_id,
        clinic_id=entry.clinic_id,
        entry_id=entry.id,
        version_no=version_no,
        title_ciphertext=field_codec.encrypt_text(
            entry.clinic_id, "entry_version.title", version_id, title
        ),
        content_ciphertext=field_codec.encrypt_text(
            entry.clinic_id, "entry_version.content", version_id, content
        ),
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        patient_facing=patient_facing,
        author_id=author_id,
        reverted_from_version_id=reverted_from_version_id,
    )


def decrypt_version(version: EntryVersion) -> tuple[str, str]:
    if version.title_ciphertext is None or version.content_ciphertext is None:
        raise HTTPException(
            status_code=409, detail="Archived version must be rehydrated"
        )
    return (
        field_codec.decrypt_text(
            version.clinic_id,
            "entry_version.title",
            version.id,
            version.title_ciphertext,
        ),
        field_codec.decrypt_text(
            version.clinic_id,
            "entry_version.content",
            version.id,
            version.content_ciphertext,
        ),
    )


def version_public(version: EntryVersion) -> EntryVersionPublic:
    title, content = decrypt_version(version)
    return EntryVersionPublic(
        id=version.id,
        entry_id=version.entry_id,
        version_no=version.version_no,
        title=title,
        content=content,
        content_sha256=version.content_sha256,
        author_id=version.author_id,
        reverted_from_version_id=version.reverted_from_version_id,
        created_at=version.created_at,
    )


def entry_public(session: Session, entry: Entry) -> EntryPublic:
    if entry.current_version_id is None:
        raise HTTPException(status_code=500, detail="Entry has no current version")
    version = session.get(EntryVersion, entry.current_version_id)
    if version is None or version.clinic_id != entry.clinic_id:
        raise HTTPException(status_code=500, detail="Entry version missing")
    public = version_public(version)
    return EntryPublic(
        id=entry.id,
        clinic_id=entry.clinic_id,
        patient_id=entry.patient_id,
        section=entry.section,
        origin=entry.origin,
        patient_facing=entry.patient_facing,
        version_id=version.id,
        version_no=version.version_no,
        title=public.title,
        content=public.content,
        author_id=version.author_id,
        created_at=entry.created_at,
    )


def create_entry(
    session: Session, context: RequestContext, data: EntryCreate
) -> EntryPublic:
    get_patient(session, context, data.patient_id)
    origin, patient_facing = authorize_entry_create(context, data)
    entry = Entry(
        clinic_id=context.clinic_id,
        patient_id=data.patient_id,
        section=data.section,
        origin=origin,
        patient_facing=patient_facing,
        source_job_id=context.job_id if context.role == "worker" else None,
    )
    session.add(entry)
    session.flush()
    version = _new_version(
        entry=entry,
        version_no=1,
        title=data.title,
        content=data.content,
        author_id=context.user_id,
        patient_facing=patient_facing,
    )
    session.add(version)
    session.flush()
    entry.current_version_id = version.id
    session.add(entry)

    if data.supersedes_entry_id or data.conflicts_with_entry_id:
        if context.role != "clinician":
            raise HTTPException(
                status_code=403, detail="Only clinicians create corrections"
            )
        for target_id, relation_type in (
            (data.supersedes_entry_id, "supersedes"),
            (data.conflicts_with_entry_id, "conflicts_with"),
        ):
            if target_id is None:
                continue
            target = get_scoped_entry(session, context, target_id)
            if target.patient_id != entry.patient_id:
                raise HTTPException(status_code=404, detail="Related entry not found")
            session.add(
                EntryRelation(
                    clinic_id=context.clinic_id,
                    source_entry_id=entry.id,
                    target_entry_id=target.id,
                    relation_type=relation_type,
                    created_by_id=context.user_id,
                )
            )
    emit_change(
        session,
        context,
        action="entry.created",
        resource_type="entry",
        resource_id=entry.id,
        metadata={"version_id": str(version.id), "section": entry.section},
    )
    session.commit()
    session.refresh(entry)
    return entry_public(session, entry)


def patch_entry(
    session: Session,
    context: RequestContext,
    entry_id: uuid.UUID,
    *,
    if_match: str,
    title: str | None,
    content: str | None,
    patient_facing: bool | None,
    reverted_from_version_id: uuid.UUID | None = None,
    action: str = "entry.updated",
) -> EntryPublic:
    entry = get_scoped_entry(session, context, entry_id, lock=True)
    authorize_entry_write(context, entry)
    if entry.current_version_id is None:
        raise HTTPException(status_code=500, detail="Entry has no current version")
    if normalize_etag(if_match) != str(entry.current_version_id):
        raise VersionConflictError(entry.current_version_id)
    current = session.get(EntryVersion, entry.current_version_id)
    if current is None:
        raise HTTPException(status_code=500, detail="Entry version missing")
    current_title, current_content = decrypt_version(current)
    effective_patient_facing = (
        patient_facing if patient_facing is not None else entry.patient_facing
    )
    if context.role == "patient":
        effective_patient_facing = True
    next_version = _new_version(
        entry=entry,
        version_no=current.version_no + 1,
        title=title if title is not None else current_title,
        content=content if content is not None else current_content,
        author_id=context.user_id,
        patient_facing=effective_patient_facing,
        reverted_from_version_id=reverted_from_version_id,
    )
    session.add(next_version)
    session.flush()
    entry.current_version_id = next_version.id
    entry.patient_facing = effective_patient_facing
    session.add(entry)
    affected_patients: set[uuid.UUID] = set()
    if context.role in {"staff", "clinician"}:
        related_highlights = session.exec(
            select(Highlight).where(
                Highlight.clinic_id == context.clinic_id,
                Highlight.entry_id == entry.id,
            )
        ).all()
        for highlight in related_highlights:
            _, affected = record_feedback(
                session,
                context,
                highlight,
                signal="edit",
                idempotency_key=f"edit:{next_version.id}:highlight:{highlight.id}",
            )
            affected_patients.update(affected)
    metadata = {
        "previous_version_id": str(current.id),
        "version_id": str(next_version.id),
    }
    if reverted_from_version_id:
        metadata["reverted_from_version_id"] = str(reverted_from_version_id)
    emit_change(
        session,
        context,
        action=action,
        resource_type="entry",
        resource_id=entry.id,
        metadata=metadata,
    )
    for patient_id in affected_patients:
        rebuild_glance(session, context, patient_id)
    session.commit()
    session.refresh(entry)
    return entry_public(session, entry)


def versions_for_entry(
    session: Session, context: RequestContext, entry_id: uuid.UUID
) -> list[EntryVersionPublic]:
    get_scoped_entry(session, context, entry_id)
    versions = session.exec(
        select(EntryVersion)
        .where(
            EntryVersion.clinic_id == context.clinic_id,
            EntryVersion.entry_id == entry_id,
        )
        .order_by(col(EntryVersion.version_no))
    ).all()
    return [version_public(version) for version in versions]


def get_scoped_version(
    session: Session,
    context: RequestContext,
    entry: Entry,
    version_id: uuid.UUID,
) -> EntryVersion:
    version = session.exec(
        select(EntryVersion).where(
            EntryVersion.id == version_id,
            EntryVersion.entry_id == entry.id,
            EntryVersion.clinic_id == context.clinic_id,
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return version


def diff_versions(
    session: Session,
    context: RequestContext,
    entry_id: uuid.UUID,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
) -> str:
    entry = get_scoped_entry(session, context, entry_id)
    from_version = get_scoped_version(session, context, entry, from_id)
    to_version = get_scoped_version(session, context, entry, to_id)
    _, from_content = decrypt_version(from_version)
    _, to_content = decrypt_version(to_version)
    return "".join(
        difflib.unified_diff(
            from_content.splitlines(keepends=True),
            to_content.splitlines(keepends=True),
            fromfile=str(from_id),
            tofile=str(to_id),
            lineterm="\n",
        )
    )


def timeline(
    session: Session, context: RequestContext, patient_id: uuid.UUID
) -> list[PatientTimelineEntry]:
    get_patient(session, context, patient_id)
    statement = select(Entry).where(
        Entry.clinic_id == context.clinic_id, Entry.patient_id == patient_id
    )
    if context.role == "patient":
        statement = statement.where(
            col(Entry.patient_facing).is_(True),
            col(Entry.origin).not_in({"ai", "system"}),
        )
    entries = session.exec(statement.order_by(desc(col(Entry.created_at)))).all()
    output: list[PatientTimelineEntry] = []
    for entry in entries:
        public = entry_public(session, entry)
        output.append(
            PatientTimelineEntry(
                id=public.id,
                patient_id=public.patient_id,
                section=public.section,
                patient_facing=public.patient_facing,
                version_id=public.version_id,
                version_no=public.version_no,
                title=public.title,
                content=public.content,
                created_at=public.created_at,
            )
        )
    return output


def validate_anchor(
    content: str,
    *,
    start_offset: int,
    end_offset: int,
    exact_quote: str,
    prefix: str,
    suffix: str,
    quote_sha256: str,
) -> tuple[str, bool]:
    valid = (
        0 <= start_offset <= end_offset <= len(content)
        and content[start_offset:end_offset] == exact_quote
        and hashlib.sha256(exact_quote.encode()).hexdigest() == quote_sha256
        and content[max(0, start_offset - len(prefix)) : start_offset] == prefix
        and content[end_offset : end_offset + len(suffix)] == suffix
    )
    return ("resolved", False) if valid else ("orphaned", True)


def resolve_pointer(
    pointer: ProvenancePointer, version: EntryVersion
) -> dict[str, object]:
    _, content = decrypt_version(version)
    exact_quote = field_codec.decrypt_text(
        pointer.clinic_id,
        "provenance.exact_quote",
        pointer.id,
        pointer.exact_quote_ciphertext,
    )
    prefix = field_codec.decrypt_text(
        pointer.clinic_id, "provenance.prefix", pointer.id, pointer.prefix_ciphertext
    )
    suffix = field_codec.decrypt_text(
        pointer.clinic_id, "provenance.suffix", pointer.id, pointer.suffix_ciphertext
    )
    state, review_required = validate_anchor(
        content,
        start_offset=pointer.start_offset,
        end_offset=pointer.end_offset,
        exact_quote=exact_quote,
        prefix=prefix,
        suffix=suffix,
        quote_sha256=pointer.quote_sha256,
    )
    return {
        "id": pointer.id,
        "entry_version_id": pointer.entry_version_id,
        "state": state,
        "review_required": review_required,
        "start_offset": pointer.start_offset,
        "end_offset": pointer.end_offset,
        "exact_quote": exact_quote,
        "prefix": prefix,
        "suffix": suffix,
        "quote_sha256": pointer.quote_sha256,
        "audio_asset_id": pointer.audio_asset_id,
        "audio_start_ms": pointer.audio_start_ms,
        "audio_end_ms": pointer.audio_end_ms,
    }


def rebuild_glance(
    session: Session, context: RequestContext, patient_id: uuid.UUID
) -> PatientGlanceSnapshot:
    get_patient(session, context, patient_id)
    eligible = session.exec(
        select(Highlight).where(
            Highlight.clinic_id == context.clinic_id,
            Highlight.patient_id == patient_id,
            (col(Highlight.status) == "accepted") | col(Highlight.pinned).is_(True),
            Highlight.anchor_state == "resolved",
            col(Highlight.review_required).is_(False),
        )
    ).all()
    score_components = {
        highlight.id: refresh_highlight_score(session, highlight).components
        for highlight in eligible
    }
    session.flush()
    highlights = session.exec(
        select(Highlight)
        .where(
            Highlight.clinic_id == context.clinic_id,
            Highlight.patient_id == patient_id,
            (col(Highlight.status) == "accepted") | col(Highlight.pinned).is_(True),
            Highlight.anchor_state == "resolved",
            col(Highlight.review_required).is_(False),
        )
        .order_by(
            desc(col(Highlight.pinned)),
            desc(col(Highlight.critical)),
            desc(col(Highlight.unresolved)),
            desc(col(Highlight.clinician_confirmed)),
            desc(col(Highlight.final_score)),
            desc(col(Highlight.created_at)),
        )
        .limit(5)
    ).all()
    cards: list[dict[str, object]] = []
    for highlight in highlights:
        pointer = session.exec(
            select(ProvenancePointer).where(
                ProvenancePointer.clinic_id == context.clinic_id,
                ProvenancePointer.highlight_id == highlight.id,
            )
        ).first()
        if pointer is None:
            continue
        if pointer.anchor_state != "resolved" or pointer.review_required:
            continue
        cards.append(
            {
                "highlight_id": str(highlight.id),
                "label": field_codec.decrypt_text(
                    highlight.clinic_id,
                    "highlight.label",
                    highlight.id,
                    highlight.label_ciphertext,
                ),
                "critical": highlight.critical,
                "pinned": highlight.pinned,
                "patient_facing": highlight.patient_facing,
                "risk_reason": highlight.risk_reason,
                "score_components": score_components.get(highlight.id, {}),
                "provenance_pointer_id": str(pointer.id),
            }
        )
    snapshot = session.exec(
        select(PatientGlanceSnapshot).where(
            PatientGlanceSnapshot.clinic_id == context.clinic_id,
            PatientGlanceSnapshot.patient_id == patient_id,
        )
    ).first()
    if snapshot is None:
        snapshot = PatientGlanceSnapshot(
            clinic_id=context.clinic_id,
            patient_id=patient_id,
            payload_ciphertext=b"pending",
        )
    snapshot.generated_at = get_datetime_utc()
    snapshot.payload_ciphertext = field_codec.encrypt_json(
        context.clinic_id, "glance.payload", snapshot.id, {"cards": cards}
    )
    session.add(snapshot)
    return snapshot


def read_glance(
    snapshot: PatientGlanceSnapshot,
) -> tuple[list[dict[str, object]], datetime]:
    payload = cast(
        dict[str, object],
        field_codec.decrypt_json(
            snapshot.clinic_id,
            "glance.payload",
            snapshot.id,
            snapshot.payload_ciphertext,
        ),
    )
    cards = payload.get("cards", [])
    if not isinstance(cards, list):
        return [], snapshot.generated_at
    return cast(list[dict[str, object]], cards[:5]), snapshot.generated_at
