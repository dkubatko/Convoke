from fastapi import APIRouter

from app.api import auth, bots, chats, health, mcp, models, settings, workflows

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(bots.router, tags=["bots"])
api_router.include_router(chats.router, tags=["chats"])
api_router.include_router(models.router, tags=["models"])
api_router.include_router(mcp.router, tags=["mcp"])
api_router.include_router(mcp.public_router, tags=["mcp"])
api_router.include_router(workflows.router, tags=["workflows"])
api_router.include_router(settings.router, tags=["settings"])
