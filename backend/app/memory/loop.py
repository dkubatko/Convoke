"""Worker loop: close conversation segments into chunks, embed pending ones."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.runtime_settings import effective_settings
from app.memory.chunker import chunk_chat, chunk_token_budget
from app.memory.embeddings import Embedder
from app.memory.store import embed_pending_chunks
from app.memory.runtime import embedding_state_for
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
        async with self.sessionmaker() as session:
            # A model swap owns chunking AND embedding while it runs: it is
            # about to re-cut history with the new tokenizer/budget, so cutting
            # more chunks here would only produce work it deletes.
            state = await embedding_state_for(session, "memory")
            if state is not None and state.status == "reembedding":
                return
            effective = await effective_settings(session, self.settings)
            lull = timedelta(seconds=effective.chunk_lull_seconds)
            budget = chunk_token_budget(effective, state)
            chat_ids = (
                await session.execute(select(Chat.id).where(Chat.status == "authorized"))
            ).scalars().all()
            made = 0
            for chat_id in chat_ids:
                made += await chunk_chat(
                    session,
                    chat_id,
                    self.embedder,
                    now,
                    lull,
                    budget,
                    effective.chunk_max_messages,
                    effective.chunk_overlap_messages,
                )
            if made:
                log.info("chunked %d new segments", made)
            await session.commit()

        # Embed in a separate transaction; keeps chunk commits fast.
        while True:
            async with self.sessionmaker() as session:
                # Re-check each batch: a swap can start mid-drain, and the
                # re-embed job will pick pending chunks up itself.
                state = await embedding_state_for(session, "memory")
                if state is not None and state.status == "reembedding":
                    return
                n = await embed_pending_chunks(
                    session, self.embedder, self.settings.embedding_batch_size
                )
                await session.commit()
            if n:
                log.info("embedded %d chunks", n)
            if n < self.settings.embedding_batch_size:
                break
