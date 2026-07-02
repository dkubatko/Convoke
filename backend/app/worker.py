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
from app.memory.loop import MemoryLoop
from app.memory.runtime import get_embedder
from app.telegram.consumer import InboxConsumer
from app.telegram.gateway import Gateway
from app.telegram.limiter import SendLimiter

log = logging.getLogger("convoke.worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    lock_conn = await acquire_singleton_lock(SINGLETON_LOCK_WORKER)
    log.info("worker started (singleton lock acquired)")
    sessionmaker = get_sessionmaker()
    try:
        embedder = get_embedder()
        limiter = SendLimiter()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(Gateway(sessionmaker).run(), name="gateway")
            tg.create_task(InboxConsumer(sessionmaker).run(), name="inbox-consumer")
            tg.create_task(MemoryLoop(sessionmaker, embedder).run(), name="memory-loop")
            tg.create_task(AgentLoop(sessionmaker, embedder, limiter).run(), name="agent-loop")
    finally:
        await lock_conn.close()
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
