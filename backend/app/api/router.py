from fastapi import APIRouter

from app.api.v1 import artifacts, health, runs

api_router = APIRouter()
api_router.include_router(health.router, prefix="/v1")
api_router.include_router(runs.router, prefix="/v1")
api_router.include_router(artifacts.router, prefix="/v1")
