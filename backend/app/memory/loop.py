"""Worker loop: close conversation segments into chunks, embed pending ones."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.memory.chunker import chunk_chat
from app.memory.embeddings import Embedder
from app.memory.store import embed_pending_chunks
from app.models import Chat

log = logging.getLogger("convoke.memory")

TICK_S = 20


class MemoryLoop:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.settings = get_settings()

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 — the loop must survive transient failures
                log.exception("memory tick failed")
            await asyncio.sleep(TICK_S)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        lull = timedelta(seconds=self.settings.chunk_lull_seconds)
        async with self.sessionmaker() as session:
            chat_ids = (
                await session.execute(select(Chat.id).where(Chat.status == "authorized"))
            ).scalars().all()
            made = 0
            for chat_id in chat_ids:
                made += await chunk_chat(
                    session,
                    chat_id,
                    now,
                    lull,
                    self.settings.chunk_max_messages,
                    self.settings.chunk_overlap_messages,
                )
            if made:
                log.info("chunked %d new segments", made)
            await session.commit()

        # Embed in a separate transaction; keeps chunk commits fast.
        while True:
            async with self.sessionmaker() as session:
                n = await embed_pending_chunks(
                    session, self.embedder, self.settings.embedding_batch_size
                )
                await session.commit()
            if n:
                log.info("embedded %d chunks", n)
            if n < self.settings.embedding_batch_size:
                break
