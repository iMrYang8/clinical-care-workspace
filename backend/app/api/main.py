from fastapi import APIRouter

from app.api.routes import (
    admin,
    ai,
    auth,
    collaboration,
    decay,
    entries,
    events,
    patients,
    trust,
    utils,
    voice,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(ai.router)
api_router.include_router(utils.router)
api_router.include_router(patients.router)
api_router.include_router(entries.router)
api_router.include_router(collaboration.router)
api_router.include_router(trust.router)
api_router.include_router(events.router)
api_router.include_router(decay.router)
api_router.include_router(voice.router)
