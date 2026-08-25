from fastapi import APIRouter

from app.api.routes import auth, collaboration, entries, events, patients, trust, utils

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(utils.router)
api_router.include_router(patients.router)
api_router.include_router(entries.router)
api_router.include_router(collaboration.router)
api_router.include_router(trust.router)
api_router.include_router(events.router)
