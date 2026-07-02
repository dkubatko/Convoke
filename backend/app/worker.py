"""Convoke worker: singleton process for DB-driven consumers.

Will host (from M2 onward): inbox consumers, embedding pipeline, intent
trigger evaluation, agent runs, and the scheduled-workflow tick loop.
For now it holds the singleton lock and heartbeats.
"""

import asyncio
import logging

from app.core.db import SINGLETON_LOCK_WORKER, acquire_singleton_lock, get_engine

log = logging.getLogger("convoke.worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    lock_conn = await acquire_singleton_lock(SINGLETON_LOCK_WORKER)
    log.info("worker started (singleton lock acquired)")
    try:
        while True:
            await asyncio.sleep(30)
            log.info("heartbeat")
    finally:
        await lock_conn.close()
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
