"""Worker loop dispatching pending agent runs.

Runs execute concurrently across chats (bounded) but strictly serialized
within a chat — a mention arriving while a workflow fires must not interleave
replies or race on notes.
"""

import asyncio
import logging

from aiogram import Bot as AiogramBot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.runtime import execute_run
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.memory.embeddings import Embedder
from app.models import AgentRun, Bot, Chat
from app.telegram.client import BotCache
from app.telegram.limiter import SendLimiter

log = logging.getLogger("convoke.agent-loop")

POLL_S = 1.0


class AgentLoop:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        limiter: SendLimiter,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.limiter = limiter
        self._bots = BotCache()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(get_settings().agent_concurrency)
        self._in_flight: set[int] = set()

    async def _bot_for(self, session: AsyncSession, bot_id: int) -> AiogramBot:
        bot_row = await session.get(Bot, bot_id)
        return self._bots.get(bot_id, bot_row.token_encrypted, decrypt(bot_row.token_encrypted))

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self._dispatch_pending()
                except Exception:  # noqa: BLE001 — one bad row must not kill the loop
                    log.exception("agent dispatch failed")
                await asyncio.sleep(POLL_S)
        finally:
            await self._bots.aclose()

    async def _dispatch_pending(self) -> None:
        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
                    select(AgentRun.id, AgentRun.chat_id, Chat.bot_id)
                    .join(Chat, Chat.id == AgentRun.chat_id)
                    .where(AgentRun.status == "pending")
                    .order_by(AgentRun.id)
                    .limit(20)
                )
            ).all()
            for run_id, chat_id, bot_id in rows:
                if run_id in self._in_flight:
                    continue
                try:
                    bot = await self._bot_for(session, bot_id)
                except Exception as e:  # noqa: BLE001 — unusable bot: fail the run, not the loop
                    log.exception("cannot build bot %s for run %s", bot_id, run_id)
                    run = await session.get(AgentRun, run_id)
                    run.status = "error"
                    run.error = f"bot unusable: {type(e).__name__}: {e}"
                    await session.commit()
                    continue
                self._in_flight.add(run_id)
                asyncio.create_task(
                    self._run_locked(run_id, chat_id, bot), name=f"agent-run-{run_id}"
                )

    async def _run_locked(self, run_id: int, chat_id: int, bot: AiogramBot) -> None:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        try:
            # Ordering matters: chat lock BEFORE the global slot. Runs queued
            # behind a busy chat must wait without holding a concurrency slot,
            # or one chatty chat's backlog can occupy every slot and starve
            # the other chats.
            async with lock:
                async with self._semaphore:
                    await execute_run(
                        self.sessionmaker, self.embedder, self.limiter, bot, run_id
                    )
        except Exception:  # noqa: BLE001 — loop must survive
            log.exception("agent run %s crashed", run_id)
        finally:
            self._in_flight.discard(run_id)
