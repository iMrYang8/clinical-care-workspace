import uuid

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.deps import CurrentContext, SessionDep
from app.core.config import settings
from app.models import (
    DecayArchivePublic,
    DecayArchiveRequest,
    DecayCandidatePublic,
    DecayPreviewPublic,
    DecayRun,
    EntryVersion,
    RehydratePublic,
    get_datetime_utc,
)
from app.services.decay import (
    POLICY_VERSION,
    archive_version,
    list_decay_candidates,
    rehydrate_version,
)
from app.services.nightingale import emit_change

router = APIRouter(prefix="/decay", tags=["decay"])


def _require_decay_role(context: CurrentContext, *, write: bool = False) -> None:
    if not settings.DATA_DECAY_ENABLED:
        raise HTTPException(status_code=503, detail="Data decay is disabled")
    if context.role not in {"clinician", "admin"}:
        raise HTTPException(status_code=403, detail="Role cannot manage retention")
    if write and context.role != "clinician":
        raise HTTPException(status_code=403, detail="Clinical role required")


@router.get("/preview", response_model=DecayPreviewPublic)
def preview(session: SessionDep, context: CurrentContext) -> DecayPreviewPublic:
    _require_decay_role(context)
    candidates = list_decay_candidates(session, context)
    return DecayPreviewPublic(
        candidates=[
            DecayCandidatePublic.model_validate(item.__dict__) for item in candidates
        ],
        count=len(candidates),
    )


@router.post("/archive", response_model=DecayArchivePublic)
def archive(
    body: DecayArchiveRequest,
    session: SessionDep,
    context: CurrentContext,
) -> DecayArchivePublic:
    _require_decay_role(context, write=True)
    candidates = list_decay_candidates(session, context)
    eligible_ids = {
        candidate.entry_version_id
        for candidate in candidates
        if candidate.eligible_for_cold
    }
    requested = set(body.entry_version_ids) if body.entry_version_ids else eligible_ids
    selected = requested & eligible_ids
    if requested - eligible_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DECAY_NOT_ELIGIBLE",
                "entry_version_ids": sorted(
                    str(item) for item in requested - eligible_ids
                ),
            },
        )
    run = DecayRun(
        clinic_id=context.clinic_id,
        cutoff_at=get_datetime_utc(),
        dry_run=body.dry_run or settings.DATA_DECAY_DRY_RUN,
        candidate_count=len(selected),
        created_by_id=context.user_id,
    )
    session.add(run)
    session.flush()
    if not run.dry_run:
        for version_id in sorted(selected, key=str):
            version = session.exec(
                select(EntryVersion).where(
                    EntryVersion.clinic_id == context.clinic_id,
                    EntryVersion.id == version_id,
                )
            ).first()
            if version is None:
                raise HTTPException(status_code=404, detail="Version not found")
            archive_version(session, context, version)
            run.archived_count += 1
            emit_change(
                session,
                context,
                action="entry_version.archived",
                resource_type="entry_version",
                resource_id=version.id,
                metadata={"storage_tier": "cold", "policy": POLICY_VERSION},
            )
    emit_change(
        session,
        context,
        action="decay.completed" if not run.dry_run else "decay.previewed",
        resource_type="decay_run",
        resource_id=run.id,
        metadata={
            "candidate_count": run.candidate_count,
            "archived_count": run.archived_count,
        },
    )
    session.add(run)
    session.commit()
    return DecayArchivePublic(
        decay_run_id=run.id,
        candidate_count=run.candidate_count,
        archived_count=run.archived_count,
        error_count=run.error_count,
    )


@router.post("/entries/{version_id}/rehydrate", response_model=RehydratePublic)
def rehydrate(
    version_id: uuid.UUID, session: SessionDep, context: CurrentContext
) -> RehydratePublic:
    _require_decay_role(context, write=True)
    version = session.exec(
        select(EntryVersion).where(
            EntryVersion.clinic_id == context.clinic_id,
            EntryVersion.id == version_id,
        )
    ).first()
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    version = rehydrate_version(session, context, version)
    emit_change(
        session,
        context,
        action="entry_version.rehydrated",
        resource_type="entry_version",
        resource_id=version.id,
        metadata={"storage_tier": version.storage_tier},
    )
    session.commit()
    session.refresh(version)
    return RehydratePublic(
        entry_version_id=version.id,
        storage_tier=version.storage_tier,
        content_sha256=version.content_sha256,
    )
