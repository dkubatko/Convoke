import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import update

from app.api.router import api_router
from app.core.db import (
    SINGLETON_LOCK_BACKEND,
    acquire_singleton_lock,
    get_engine,
    get_sessionmaker,
)

log = logging.getLogger("convoke.backend")


async def _recover_interrupted_imports() -> None:
    """Import tasks run in this (backend) process; a restart kills them mid-run,
    stranding the job in a non-terminal state — which then blocks every future
    import for that chat via the 409 guard. Since we're the singleton backend,
    any such job at startup was interrupted: fail it so the chat unblocks."""
    from app.models import ImportJob

    async with get_sessionmaker()() as session:
        result = await session.execute(
            update(ImportJob)
            .where(ImportJob.status.in_(("pending", "validating", "ingesting")))
            .values(
                status="failed",
                detail="interrupted by a backend restart — re-upload to retry",
                finished_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        if result.rowcount:
            log.warning("failed %d import job(s) interrupted by restart", result.rowcount)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app.state.singleton_lock_conn = await acquire_singleton_lock(SINGLETON_LOCK_BACKEND)
    await _recover_interrupted_imports()
    yield
    await app.state.singleton_lock_conn.close()
    await get_engine().dispose()


app = FastAPI(title="Convoke", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
