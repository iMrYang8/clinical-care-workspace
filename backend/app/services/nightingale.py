import difflib
import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import desc
from sqlmodel import Session, col, select

from app.api.deps import RequestContext
from app.core.field_crypto import field_codec
from app.models import (
    AuditEvent,
    ClinicMembership,
    DecisionAssessment,
    DomainEvent,
    Entry,
    EntryCreate,
    EntryPublic,
    EntryRelation,
    EntryType,
    EntryVersion,
    EntryVersionPublic,
    Highlight,
    Job,
    JobAttempt,
    Patient,
    PatientGlanceSnapshot,
    PatientIdentifier,
    PatientPublic,
    PatientPublication,
    PatientPublicationItem,
    PatientSharingRequest,
    PatientTimelineEntry,
    PatientUserLink,
    PatientVisit,
    ProvenancePointer,
    User,
    get_datetime_utc,
)
from app.services.decisioning import (
    assessment_review_state,
    decision_payload,
    redaction_is_qualified,
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


def list_patients(
    session: Session,
    context: RequestContext,
    *,
    search: str | None = None,
    visit_scope: Literal["all", "today", "previous"] = "all",
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[PatientPublic], int]:
    """Return a tenant-scoped, decrypted patient directory page.

    Names, dates of birth, and MRNs remain encrypted at rest, so filtering is
    deliberately performed only after the trusted API has applied tenant/RLS
    scope.  This is bounded to one clinic and avoids introducing a plaintext
    search index for clinical identity data.
    """

    statement = select(Patient).where(Patient.clinic_id == context.clinic_id)
    if context.role == "patient":
        statement = statement.join(PatientUserLink).where(
            PatientUserLink.clinic_id == context.clinic_id,
            PatientUserLink.user_id == context.user_id,
        )
    patients = list(session.exec(statement.order_by(col(Patient.created_at))).all())
    patient_ids = [patient.id for patient in patients]
    identifiers = (
        session.exec(
            select(PatientIdentifier).where(
                PatientIdentifier.clinic_id == context.clinic_id,
                col(PatientIdentifier.patient_id).in_(patient_ids),
            )
        ).all()
        if patient_ids
        else []
    )
    singapore = ZoneInfo("Asia/Singapore")
    singapore_today = datetime.now(singapore).date()
    day_start = datetime.combine(
        singapore_today, time.min, tzinfo=singapore
    ).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    visits = (
        session.exec(
            select(PatientVisit)
            .where(
                PatientVisit.clinic_id == context.clinic_id,
                col(PatientVisit.patient_id).in_(patient_ids),
                PatientVisit.scheduled_at >= day_start,
                PatientVisit.scheduled_at < day_end,
                col(PatientVisit.status).notin_({"cancelled", "no_show"}),
            )
            .order_by(col(PatientVisit.scheduled_at))
        ).all()
        if patient_ids
        else []
    )
    today_visit_by_patient: dict[uuid.UUID, PatientVisit] = {}
    for visit in visits:
        today_visit_by_patient.setdefault(visit.patient_id, visit)

    latest_activity_by_patient: dict[uuid.UUID, datetime] = {}
    if patient_ids:
        activity_rows = session.exec(
            select(Entry.patient_id, Entry.occurred_at).where(
                Entry.clinic_id == context.clinic_id,
                col(Entry.patient_id).in_(patient_ids),
            )
        ).all()
        for patient_id, occurred_at in activity_rows:
            current = latest_activity_by_patient.get(patient_id)
            if current is None or occurred_at > current:
                latest_activity_by_patient[patient_id] = occurred_at
    mrn_by_patient: dict[uuid.UUID, str] = {}
    for identifier in identifiers:
        if identifier.identifier_type == "medical_record_number":
            mrn_by_patient[identifier.patient_id] = field_codec.decrypt_text(
                identifier.clinic_id,
                "patient_identifier.value",
                identifier.id,
                identifier.value_ciphertext,
            )

    rows: list[PatientPublic] = []
    name_counts: dict[str, int] = {}
    for patient in patients:
        display_name = field_codec.decrypt_text(
            patient.clinic_id,
            "patient.display_name",
            patient.id,
            patient.display_name_ciphertext,
        )
        key = " ".join(display_name.casefold().split())
        name_counts[key] = name_counts.get(key, 0) + 1
        dob: date | None = None
        if patient.date_of_birth_ciphertext:
            dob = date.fromisoformat(
                field_codec.decrypt_text(
                    patient.clinic_id,
                    "patient.date_of_birth",
                    patient.id,
                    patient.date_of_birth_ciphertext,
                )
            )
        today_visit = today_visit_by_patient.get(patient.id)
        rows.append(
            PatientPublic(
                id=patient.id,
                display_name=display_name,
                date_of_birth=dob,
                medical_record_number=mrn_by_patient.get(patient.id),
                today_visit_at=(
                    today_visit.scheduled_at if today_visit is not None else None
                ),
                today_visit_status=(
                    today_visit.status if today_visit is not None else None
                ),
                today_visit_type=(
                    today_visit.visit_type if today_visit is not None else None
                ),
                last_activity_at=latest_activity_by_patient.get(
                    patient.id, patient.created_at
                ),
            )
        )
    for row in rows:
        row.same_name_count = name_counts[" ".join(row.display_name.casefold().split())]

    normalized_search = " ".join((search or "").casefold().split())
    if normalized_search:
        rows = [
            row
            for row in rows
            if normalized_search in row.display_name.casefold()
            or normalized_search in (row.medical_record_number or "").casefold()
            or normalized_search
            in (row.date_of_birth.isoformat() if row.date_of_birth else "")
        ]
    if visit_scope == "today":
        rows = [row for row in rows if row.today_visit_at is not None]
    elif visit_scope == "previous":
        rows = [row for row in rows if row.today_visit_at is None]

    if visit_scope == "today":
        rows.sort(
            key=lambda row: (
                row.today_visit_at.timestamp() if row.today_visit_at else float("inf"),
                row.display_name.casefold(),
                str(row.id),
            )
        )
    elif visit_scope == "previous":
        rows.sort(
            key=lambda row: (
                -row.last_activity_at.timestamp() if row.last_activity_at else 0,
                row.display_name.casefold(),
                str(row.id),
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                0 if row.today_visit_at is not None else 1,
                row.today_visit_at.timestamp()
                if row.today_visit_at is not None
                else -(
                    row.last_activity_at.timestamp() if row.last_activity_at else 0
                ),
                row.display_name.casefold(),
                str(row.id),
            )
        )
    total = len(rows)
    return rows[offset : offset + limit], total


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
) -> tuple[str, bool, str]:
    role = context.role
    if role == "staff" and data.section == "staff":
        return "human", False, "manual_staff_note"
    if role == "clinician" and data.section == "clinician":
        return "human", data.patient_facing, "manual_clinician_note"
    if role == "patient" and data.section == "patient":
        return "human", True, "manual_patient_insight"
    if (
        role == "worker"
        and context.job_id is not None
        and data.section == "system"
        and data.origin in {"ai", "system"}
    ):
        if data.origin == "ai":
            allowed_ai_types = {
                "ai_doctor_consult_summary",
                "ai_nurse_consult_summary",
                "ai_patient_session_summary",
            }
            entry_type = data.entry_type or "ai_doctor_consult_summary"
            if entry_type not in allowed_ai_types:
                raise HTTPException(status_code=422, detail="Invalid AI entry type")
            return data.origin, False, entry_type
        return data.origin, False, "system_record"
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


def _assert_worker_job_write(
    session: Session, context: RequestContext, patient_id: uuid.UUID
) -> None:
    if context.role != "worker" or context.job_id is None:
        return
    job = session.exec(
        select(Job)
        .where(
            Job.clinic_id == context.clinic_id,
            Job.id == context.job_id,
            Job.patient_id == patient_id,
            Job.state == "running",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if (
        job is None
        or job.locked_by is None
        or job.locked_until is None
        or job.locked_until <= get_datetime_utc()
    ):
        raise HTTPException(status_code=403, detail="Active worker job required")
    try:
        attempt_id = uuid.UUID(job.locked_by)
    except ValueError:
        raise HTTPException(status_code=403, detail="Active worker job required")
    attempt = session.exec(
        select(JobAttempt).where(
            JobAttempt.clinic_id == context.clinic_id,
            JobAttempt.id == attempt_id,
            JobAttempt.job_id == job.id,
            JobAttempt.worker_membership_id == context.membership.id,
            JobAttempt.status == "started",
        )
    ).first()
    if attempt is None:
        raise HTTPException(status_code=403, detail="Active worker job required")


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
        entry_type=cast(EntryType, entry.entry_type),
        patient_facing=entry.patient_facing,
        version_id=version.id,
        version_no=version.version_no,
        title=public.title,
        content=public.content,
        author_id=version.author_id,
        created_at=entry.created_at,
        occurred_at=entry.occurred_at,
    )


def _record_clinician_publication(
    session: Session,
    context: RequestContext,
    entry: Entry,
    version: EntryVersion,
    content: str,
) -> PatientPublication:
    """Bind a clinician sharing decision to an exact immutable text span."""

    if not redaction_is_qualified(session, clinic_id=context.clinic_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "REDACTION_EVALUATION_REQUIRED"},
        )

    pointer_id = uuid.uuid4()
    pointer = ProvenancePointer(
        id=pointer_id,
        clinic_id=context.clinic_id,
        entry_version_id=version.id,
        start_offset=0,
        end_offset=len(content),
        exact_quote_ciphertext=field_codec.encrypt_text(
            context.clinic_id,
            "provenance.exact_quote",
            pointer_id,
            content,
        ),
        prefix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.prefix", pointer_id, ""
        ),
        suffix_ciphertext=field_codec.encrypt_text(
            context.clinic_id, "provenance.suffix", pointer_id, ""
        ),
        quote_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )
    session.add(pointer)
    publication = PatientPublication(
        clinic_id=context.clinic_id,
        patient_id=entry.patient_id,
        entry_version_id=version.id,
        approved_by_membership_id=context.membership.id,
    )
    session.add(publication)
    session.flush()
    session.add(
        PatientPublicationItem(
            clinic_id=context.clinic_id,
            publication_id=publication.id,
            provenance_pointer_id=pointer.id,
            support_state="human_asserted",
            confidence_band="not_applicable",
        )
    )
    return publication


def _record_patient_sharing_request(
    session: Session,
    context: RequestContext,
    entry: Entry,
    version: EntryVersion,
) -> PatientSharingRequest:
    existing = session.exec(
        select(PatientSharingRequest).where(
            PatientSharingRequest.clinic_id == context.clinic_id,
            PatientSharingRequest.entry_version_id == version.id,
            PatientSharingRequest.status == "pending",
        )
    ).first()
    if existing is not None:
        return existing
    request = PatientSharingRequest(
        clinic_id=context.clinic_id,
        patient_id=entry.patient_id,
        entry_id=entry.id,
        entry_version_id=version.id,
        requested_by_membership_id=context.membership.id,
    )
    session.add(request)
    session.flush()
    return request


def create_entry(
    session: Session, context: RequestContext, data: EntryCreate
) -> EntryPublic:
    get_patient(session, context, data.patient_id)
    origin, patient_facing, entry_type = authorize_entry_create(context, data)
    _assert_worker_job_write(session, context, data.patient_id)
    entry = Entry(
        clinic_id=context.clinic_id,
        patient_id=data.patient_id,
        section=data.section,
        origin=origin,
        entry_type=entry_type,
        patient_facing=patient_facing,
        source_job_id=context.job_id if context.role == "worker" else None,
        occurred_at=data.occurred_at or get_datetime_utc(),
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

    from app.services.conflicts import detect_conflicts_for_version

    conflicts = detect_conflicts_for_version(
        session, context, entry, version, data.content
    )
    if patient_facing and conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "UNRESOLVED_CLINICAL_CONFLICT",
                "message": "Resolve the clinical conflict before patient sharing.",
            },
        )
    if patient_facing and context.role == "clinician":
        _record_clinician_publication(session, context, entry, version, data.content)

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
    if context.role == "staff" and data.patient_facing:
        sharing_request = _record_patient_sharing_request(
            session, context, entry, version
        )
        emit_change(
            session,
            context,
            action="entry.patient_sharing_requested",
            resource_type="entry",
            resource_id=sharing_request.id,
            metadata={"version_id": str(version.id), "entry_id": str(entry.id)},
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
    approved_patient_sharing: bool = False,
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
    elif context.role == "staff" and not approved_patient_sharing:
        # Clinical notes are shared through the explicit clinician publication
        # gate, never by toggling a generic entry field.
        effective_patient_facing = False
    sharing_changed = effective_patient_facing != entry.patient_facing
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
    from app.services.conflicts import detect_conflicts_for_version

    conflicts = detect_conflicts_for_version(
        session,
        context,
        entry,
        next_version,
        content if content is not None else current_content,
    )
    if effective_patient_facing and conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "UNRESOLVED_CLINICAL_CONFLICT",
                "message": "Resolve the clinical conflict before patient sharing.",
            },
        )
    if (
        effective_patient_facing
        and context.role == "clinician"
        and (sharing_changed or approved_patient_sharing)
    ):
        _record_clinician_publication(
            session,
            context,
            entry,
            next_version,
            content if content is not None else current_content,
        )
    if context.role == "staff" and patient_facing:
        sharing_request = _record_patient_sharing_request(
            session, context, entry, next_version
        )
        emit_change(
            session,
            context,
            action="entry.patient_sharing_requested",
            resource_type="patient_sharing_request",
            resource_id=sharing_request.id,
            metadata={"entry_id": str(entry.id), "version_id": str(next_version.id)},
        )
    affected_patients: set[uuid.UUID] = set()
    if sharing_changed:
        # Patient-facing Glance is precomputed. A sharing withdrawal must
        # invalidate that projection even when the entry has no feedback rows.
        affected_patients.add(entry.patient_id)
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
    if context.role == "staff" and patient_facing:
        emit_change(
            session,
            context,
            action="entry.patient_sharing_requested",
            resource_type="entry",
            resource_id=entry.id,
            metadata={"version_id": str(next_version.id)},
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
    entries = session.exec(statement.order_by(desc(col(Entry.occurred_at)))).all()
    output: list[PatientTimelineEntry] = []
    for entry in entries:
        public = entry_public(session, entry)
        publication = session.exec(
            select(PatientPublication).where(
                PatientPublication.clinic_id == context.clinic_id,
                PatientPublication.entry_version_id == public.version_id,
                col(PatientPublication.withdrawn_at).is_(None),
            )
        ).first()
        approval_receipt: dict[str, object] | None = None
        if publication is not None:
            membership = session.get(
                ClinicMembership, publication.approved_by_membership_id
            )
            approver = session.get(User, membership.user_id) if membership else None
            approval_receipt = {
                "approved_by": (
                    approver.full_name or str(approver.email)
                    if approver
                    else "Clinician"
                ),
                "approved_at": publication.approved_at.isoformat(),
                "source_title": public.title,
                "source_date": public.created_at.isoformat(),
                "withdrawal_status": "active",
            }
        output.append(
            PatientTimelineEntry(
                id=public.id,
                patient_id=public.patient_id,
                section=public.section,
                entry_type=public.entry_type,
                patient_facing=public.patient_facing,
                version_id=public.version_id,
                version_no=public.version_no,
                title=public.title,
                content=public.content,
                created_at=public.created_at,
                occurred_at=public.occurred_at,
                approval_receipt=approval_receipt,
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
        select(Highlight)
        .where(
            Highlight.clinic_id == context.clinic_id,
            Highlight.patient_id == patient_id,
            (col(Highlight.status) == "accepted") | col(Highlight.pinned).is_(True),
            Highlight.anchor_state == "resolved",
            col(Highlight.review_required).is_(False),
        )
        .execution_options(populate_existing=True)
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
        .execution_options(populate_existing=True)
    ).all()
    cards: list[dict[str, object]] = []
    review_cards: list[dict[str, object]] = []
    patient_cards: list[dict[str, object]] = []
    for highlight in highlights:
        if len(cards) >= 5 and len(patient_cards) >= 5 and len(review_cards) >= 20:
            break
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
        source_entry = session.exec(
            select(Entry).where(
                Entry.id == highlight.entry_id,
                Entry.clinic_id == context.clinic_id,
                Entry.patient_id == patient_id,
            )
        ).first()
        source_version = session.exec(
            select(EntryVersion).where(
                EntryVersion.id == highlight.source_entry_version_id,
                EntryVersion.entry_id == highlight.entry_id,
                EntryVersion.clinic_id == context.clinic_id,
            )
        ).first()
        currently_patient_facing = bool(
            highlight.patient_facing
            and source_entry is not None
            and source_entry.patient_facing
            and source_version is not None
            and source_version.patient_facing
        )
        assessment = session.exec(
            select(DecisionAssessment).where(
                DecisionAssessment.clinic_id == context.clinic_id,
                DecisionAssessment.highlight_id == highlight.id,
            )
        ).first()
        decision = decision_payload(
            assessment=assessment,
            highlight=highlight,
            score_components=score_components.get(highlight.id, {}),
        )
        card: dict[str, object] = {
            "highlight_id": str(highlight.id),
            "label": field_codec.decrypt_text(
                highlight.clinic_id,
                "highlight.label",
                highlight.id,
                highlight.label_ciphertext,
            ),
            "critical": highlight.critical,
            "pinned": highlight.pinned,
            # This is an effective projection, not a copy of the original
            # highlight flag. Withdrawing the current Entry immediately makes
            # its immutable source unsuitable for patients.
            "patient_facing": currently_patient_facing,
            "risk_reason": highlight.risk_reason,
            "score_components": score_components.get(highlight.id, {}),
            "provenance_pointer_id": str(pointer.id),
            **decision,
        }
        review_state = assessment_review_state(assessment, highlight)
        if review_state == "ready" and len(cards) < 5:
            cards.append(card)
        # Patient eligibility is applied before the independent top-five cut;
        # high-scoring internal cards therefore cannot crowd out a sixth public
        # candidate from the patient projection.
        if (
            review_state == "ready"
            and currently_patient_facing
            and len(patient_cards) < 5
        ):
            patient_cards.append(
                {
                    "highlight_id": card["highlight_id"],
                    "label": card["label"],
                    "patient_facing": True,
                    "provenance_pointer_id": card["provenance_pointer_id"],
                }
            )
        if review_state != "ready" and len(review_cards) < 20:
            review_cards.append(card)
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
        context.clinic_id,
        "glance.payload",
        snapshot.id,
        {
            "cards": cards,
            "review_cards": review_cards,
            "patient_cards": patient_cards,
        },
    )
    session.add(snapshot)
    return snapshot


def read_glance(
    snapshot: PatientGlanceSnapshot, *, patient_facing: bool = False
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
    cards = payload.get("patient_cards" if patient_facing else "cards")
    if patient_facing and cards is None:
        # Backward-compatible read for snapshots created before the independent
        # patient projection existed. Live source validation remains in the API.
        legacy_cards = payload.get("cards", [])
        cards = (
            [
                card
                for card in legacy_cards
                if isinstance(card, dict) and card.get("patient_facing") is True
            ]
            if isinstance(legacy_cards, list)
            else []
        )
    if cards is None:
        cards = []
    if not isinstance(cards, list):
        return [], snapshot.generated_at
    return cast(list[dict[str, object]], cards[:5]), snapshot.generated_at


def read_review_glance(
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
    cards = payload.get("review_cards", [])
    if not isinstance(cards, list):
        cards = []
    return cast(list[dict[str, object]], cards[:20]), snapshot.generated_at
