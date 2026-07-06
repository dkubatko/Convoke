"""Convoke worker: singleton process for Telegram polling and DB-driven consumers.

Hosts the per-bot getUpdates gateway and the inbox consumer. Later milestones
add: embedding pipeline, intent trigger evaluation, agent runs, and the
scheduled-workflow tick loop.
"""

import asyncio
import logging

from app.core.db import (
    SINGLETON_LOCK_WORKER,
    acquire_singleton_lock,
    get_engine,
    get_sessionmaker,
)
from app.agents.loop import AgentLoop
from app.intent.executor import FireExecutor
from app.intent.pipeline import IntentSweeper
from app.media.loop import MediaLoop
from app.memory.loop import MemoryLoop
from app.memory.runtime import get_embedder
from app.scheduler.loop import ScheduleLoop
from app.telegram.consumer import InboxConsumer
from app.telegram.gateway import Gateway
from app.telegram.limiter import SendLimiter

log = logging.getLogger("convoke.worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    lock_conn = await acquire_singleton_lock(SINGLETON_LOCK_WORKER)
    log.info("worker started (singleton lock acquired)")
    sessionmaker = get_sessionmaker()

    # We are the only worker: any run still 'running' was orphaned by a crash
    # or restart mid-execution. Without this it would show as running forever.
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.intent.episodes import finish_run_episode
    from app.models import AgentRun

    async with sessionmaker() as session:
        now = datetime.now(timezone.utc)
        orphaned = (
            (await session.execute(select(AgentRun).where(AgentRun.status == "running")))
            .scalars()
            .all()
        )
        for run in orphaned:
            run.status = "error"
            run.error = "interrupted by a worker restart"
            # The topic wasn't handled — its episode reverts to tracking.
            await finish_run_episode(session, run.id, None, now)
        await session.commit()
        if orphaned:
            log.warning("marked %d orphaned agent run(s) as interrupted", len(orphaned))
    try:
        embedder = get_embedder()
        limiter = SendLimiter()

        async def sweep_forever() -> None:
            from app.intent.examples import regenerate_unready

            sweeper = IntentSweeper(sessionmaker, embedder)
            ticks = 0
            while True:
                try:
                    await sweeper.sweep()
                    ticks += 1
                    if ticks % 24 == 0:  # ~every 2 minutes
                        await regenerate_unready(sessionmaker, embedder)
                except Exception:  # noqa: BLE001 — the loop must survive
                    log.exception("intent sweep failed")
                # sweep() refreshes settings with operator overrides each pass
                await asyncio.sleep(sweeper.settings.intent_sweep_interval_seconds)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(Gateway(sessionmaker).run(), name="gateway")
            tg.create_task(InboxConsumer(sessionmaker).run(), name="inbox-consumer")
            tg.create_task(MemoryLoop(sessionmaker, embedder).run(), name="memory-loop")
            tg.create_task(MediaLoop(sessionmaker).run(), name="media-loop")
            tg.create_task(AgentLoop(sessionmaker, embedder, limiter).run(), name="agent-loop")
            tg.create_task(sweep_forever(), name="intent-sweeper")
            tg.create_task(ScheduleLoop(sessionmaker).run(), name="schedule-loop")
            tg.create_task(FireExecutor(sessionmaker, limiter).run(), name="fire-executor")
    finally:
        await lock_conn.close()
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
