from fastapi import APIRouter

from app.api.routes import (
    admin,
    ai,
    auth,
    collaboration,
    decay,
    entries,
    events,
    formulary,
    notifications,
    patient_access,
    patient_registry,
    patients,
    platform,
    team,
    trust,
    utils,
    voice,
    voice_live,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(formulary.router)
api_router.include_router(ai.router)
api_router.include_router(utils.router)
api_router.include_router(patient_registry.router)
api_router.include_router(patient_registry.auth_router)
api_router.include_router(patient_access.router)
api_router.include_router(patient_access.patient_router)
api_router.include_router(platform.router)
api_router.include_router(patients.router)
api_router.include_router(team.router)
api_router.include_router(entries.router)
api_router.include_router(collaboration.router)
api_router.include_router(trust.router)
api_router.include_router(events.router)
api_router.include_router(notifications.router)
api_router.include_router(decay.router)
api_router.include_router(voice.router)
api_router.include_router(voice_live.router)
