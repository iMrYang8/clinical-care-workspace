import uuid

from fastapi import APIRouter, Header, HTTPException, Response

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    DiffPublic,
    EntryCreate,
    EntryPatch,
    EntryPublic,
    EntryVersionsPublic,
    PatientTimelineEntry,
)
from app.services.nightingale import (
    create_entry,
    decrypt_version,
    diff_versions,
    entry_public,
    get_scoped_entry,
    get_scoped_version,
    patch_entry,
    versions_for_entry,
)

router = APIRouter(prefix="/entries", tags=["entries"])


def _set_etag(response: Response, version_id: uuid.UUID) -> None:
    response.headers["ETag"] = f'"{version_id}"'


def _require_entry_read_role(context: CurrentContext) -> None:
    if context.role == "worker":
        raise HTTPException(status_code=403, detail="Role cannot read clinical entries")


def _require_version_access(
    session: SessionDep, context: CurrentContext, entry_id: uuid.UUID
) -> None:
    _require_entry_read_role(context)
    if context.role == "patient":
        entry = get_scoped_entry(session, context, entry_id)
        if entry.section != "patient":
            raise HTTPException(status_code=404, detail="Entry not found")


def _entry_response(
    context: CurrentContext, public: EntryPublic
) -> EntryPublic | PatientTimelineEntry:
    if context.role != "patient":
        return public
    return PatientTimelineEntry(
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
    )


@router.post("", status_code=201, response_model=EntryPublic | PatientTimelineEntry)
@router.post(
    "/",
    status_code=201,
    response_model=EntryPublic | PatientTimelineEntry,
    include_in_schema=False,
)
def create(
    body: EntryCreate,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
) -> EntryPublic | PatientTimelineEntry:
    created = create_entry(session, context, body)
    _set_etag(response, created.version_id)
    return _entry_response(context, created)


@router.get("/{entry_id}", response_model=EntryPublic | PatientTimelineEntry)
def read(
    entry_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
) -> EntryPublic | PatientTimelineEntry:
    _require_entry_read_role(context)
    entry = get_scoped_entry(session, context, entry_id)
    public = entry_public(session, entry)
    _set_etag(response, public.version_id)
    return _entry_response(context, public)


@router.patch("/{entry_id}", response_model=EntryPublic | PatientTimelineEntry)
def patch(
    entry_id: uuid.UUID,
    body: EntryPatch,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> EntryPublic | PatientTimelineEntry:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    updated = patch_entry(
        session,
        context,
        entry_id,
        if_match=if_match,
        title=body.title,
        content=body.content,
        patient_facing=body.patient_facing,
    )
    _set_etag(response, updated.version_id)
    return _entry_response(context, updated)


@router.get("/{entry_id}/versions", response_model=EntryVersionsPublic)
def versions(
    entry_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> EntryVersionsPublic:
    _require_version_access(session, context, entry_id)
    data = versions_for_entry(session, context, entry_id)
    return EntryVersionsPublic(data=data, count=len(data))


@router.get("/{entry_id}/versions/{version_id}/diff", response_model=DiffPublic)
def diff(
    entry_id: uuid.UUID,
    version_id: uuid.UUID,
    against: uuid.UUID,
    session: SessionDep,
    context: CurrentContext,
) -> DiffPublic:
    _require_version_access(session, context, entry_id)
    return DiffPublic(
        from_version_id=version_id,
        to_version_id=against,
        unified_diff=diff_versions(session, context, entry_id, version_id, against),
    )


@router.post(
    "/{entry_id}/versions/{version_id}/revert",
    response_model=EntryPublic | PatientTimelineEntry,
)
def revert(
    entry_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> EntryPublic | PatientTimelineEntry:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    _require_version_access(session, context, entry_id)
    entry = get_scoped_entry(session, context, entry_id)
    target = get_scoped_version(session, context, entry, version_id)
    title, content = decrypt_version(target)
    updated = patch_entry(
        session,
        context,
        entry_id,
        if_match=if_match,
        title=title,
        content=content,
        patient_facing=target.patient_facing,
        reverted_from_version_id=target.id,
        action="entry.reverted",
    )
    _set_etag(response, updated.version_id)
    return _entry_response(context, updated)
