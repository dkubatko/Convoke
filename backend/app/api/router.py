from fastapi import APIRouter

from app.api import auth, bots, chats, health, providers

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(bots.router, tags=["bots"])
api_router.include_router(chats.router, tags=["chats"])
api_router.include_router(providers.router, tags=["providers"])
