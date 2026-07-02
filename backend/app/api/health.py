from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session

router = APIRouter()


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    vector = (
        await session.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        )
    ).scalar()
    return {"status": "ok", "db": "ok", "pgvector": bool(vector)}
