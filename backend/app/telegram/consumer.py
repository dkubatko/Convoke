"""Inbox consumer: turns persisted raw updates into domain state.

Single consumer, ordered per bot by update_id. Each row is handled and marked
in its own transaction, so a crash re-processes at most the in-flight row —
handlers are idempotent to make that safe.
"""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot as AiogramBot
from aiogram.types import Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import decrypt
from app.models import Bot, InboxUpdate
from app.telegram.client import make_bot
from app.telegram.handlers import handle_update

log = logging.getLogger("convoke.consumer")

BATCH_SIZE = 50
IDLE_SLEEP_S = 1.0


class InboxConsumer:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker
        self._bots: dict[int, AiogramBot] = {}

    async def _bot_for(self, session: AsyncSession, bot_id: int) -> tuple[AiogramBot, Bot] | None:
        bot_row = await session.get(Bot, bot_id)
        if bot_row is None:
            return None
        if bot_id not in self._bots:
            self._bots[bot_id] = make_bot(decrypt(bot_row.token_encrypted))
        return self._bots[bot_id], bot_row

    async def run(self) -> None:
        try:
            while True:
                processed = await self._drain_batch()
                if processed == 0:
                    await asyncio.sleep(IDLE_SLEEP_S)
        finally:
            for bot in self._bots.values():
                await bot.session.close()

    async def _drain_batch(self) -> int:
        async with self.sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(InboxUpdate)
                        .where(InboxUpdate.processed_at.is_(None))
                        .order_by(InboxUpdate.bot_id, InboxUpdate.update_id)
                        .limit(BATCH_SIZE)
                    )
                )
                .scalars()
                .all()
            )
        for row in rows:
            await self._process_row(row.id)
        return len(rows)

    async def _process_row(self, row_id: int) -> None:
        async with self.sessionmaker() as session:
            row = await session.get(InboxUpdate, row_id)
            if row is None or row.processed_at is not None:
                return
            try:
                pair = await self._bot_for(session, row.bot_id)
                if pair is not None:
                    bot, bot_row = pair
                    update = Update.model_validate(row.payload)
                    await handle_update(session, bot, bot_row, update)
            except Exception as e:  # noqa: BLE001 — poison rows must not wedge the queue
                log.exception("update %s (bot %s) failed", row.update_id, row.bot_id)
                row.error = f"{type(e).__name__}: {e}"
            row.processed_at = datetime.now(timezone.utc)
            await session.commit()
