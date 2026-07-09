import asyncio
import logging
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import select, update

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


async def _sweep_orphaned_import_artifacts() -> None:
    """Two kinds of files strand on the imports volume across a crash: media
    dirs extracted for jobs that will never finish (the recovery above just
    failed them), and starlette upload spool files (TMPDIR points at this
    volume to keep multi-GB uploads off the docker root) whose request died
    mid-stream. Media dirs of active/done jobs are live — a done job keeps its
    bytes until the media loop describes-then-discards them."""
    from app.core.config import get_settings
    from app.models import ImportJob

    imports_dir = Path(get_settings().imports_dir)
    if not imports_dir.is_dir():
        return
    async with get_sessionmaker()() as session:
        live_jobs = set(
            (
                await session.execute(
                    select(ImportJob.id).where(
                        ImportJob.status.in_(("pending", "validating", "ingesting", "done"))
                    )
                )
            ).scalars()
        )
    spool_cutoff = time.time() - 24 * 3600  # generous: no upload streams for a day
    for entry in imports_dir.iterdir():
        m = re.fullmatch(r"job_(\d+)_media", entry.name)
        if m is not None and entry.is_dir():
            if int(m.group(1)) not in live_jobs:
                await asyncio.to_thread(shutil.rmtree, entry, True)
                log.info("removed orphaned import media dir %s", entry.name)
        elif entry.name.startswith("tmp") and entry.is_file():
            if entry.stat().st_mtime < spool_cutoff:
                entry.unlink(missing_ok=True)
                log.info("removed stale upload spool file %s", entry.name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app.state.singleton_lock_conn = await acquire_singleton_lock(SINGLETON_LOCK_BACKEND)
    await _recover_interrupted_imports()
    await _sweep_orphaned_import_artifacts()
    yield
    await app.state.singleton_lock_conn.close()
    await get_engine().dispose()


app = FastAPI(title="Convoke", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
