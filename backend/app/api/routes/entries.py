import uuid

from fastapi import APIRouter, Header, HTTPException, Response

from app.api.deps import CurrentContext, SessionDep
from app.models import (
    DiffPublic,
    EntryCreate,
    EntryPatch,
    EntryVersionsPublic,
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


@router.post("", status_code=201)
@router.post("/", status_code=201, include_in_schema=False)
def create(
    body: EntryCreate,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
):
    created = create_entry(session, context, body)
    _set_etag(response, created.version_id)
    return created


@router.get("/{entry_id}")
def read(
    entry_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
):
    entry = get_scoped_entry(session, context, entry_id)
    public = entry_public(session, entry)
    _set_etag(response, public.version_id)
    if context.role == "patient":
        return {
            "id": public.id,
            "patient_id": public.patient_id,
            "section": public.section,
            "patient_facing": public.patient_facing,
            "version_id": public.version_id,
            "version_no": public.version_no,
            "title": public.title,
            "content": public.content,
            "created_at": public.created_at,
        }
    return public


@router.patch("/{entry_id}")
def patch(
    entry_id: uuid.UUID,
    body: EntryPatch,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
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
    return updated


@router.get("/{entry_id}/versions", response_model=EntryVersionsPublic)
def versions(
    entry_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> EntryVersionsPublic:
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
    return DiffPublic(
        from_version_id=version_id,
        to_version_id=against,
        unified_diff=diff_versions(session, context, entry_id, version_id, against),
    )


@router.post("/{entry_id}/versions/{version_id}/revert")
def revert(
    entry_id: uuid.UUID,
    version_id: uuid.UUID,
    response: Response,
    session: SessionDep,
    context: CurrentContext,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
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
        patient_facing=None,
        reverted_from_version_id=target.id,
        action="entry.reverted",
    )
    _set_etag(response, updated.version_id)
    return updated
