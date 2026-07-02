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
from app.telegram.client import make_bot
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
        self._bots: dict[int, AiogramBot] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(get_settings().agent_concurrency)
        self._in_flight: set[int] = set()

    async def _bot_for(self, session: AsyncSession, bot_id: int) -> AiogramBot:
        if bot_id not in self._bots:
            bot_row = await session.get(Bot, bot_id)
            self._bots[bot_id] = make_bot(decrypt(bot_row.token_encrypted))
        return self._bots[bot_id]

    async def run(self) -> None:
        try:
            while True:
                await self._dispatch_pending()
                await asyncio.sleep(POLL_S)
        finally:
            for bot in self._bots.values():
                await bot.session.close()

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
                bot = await self._bot_for(session, bot_id)
                self._in_flight.add(run_id)
                asyncio.create_task(
                    self._run_locked(run_id, chat_id, bot), name=f"agent-run-{run_id}"
                )

    async def _run_locked(self, run_id: int, chat_id: int, bot: AiogramBot) -> None:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        try:
            async with self._semaphore, lock:
                await execute_run(self.sessionmaker, self.embedder, self.limiter, bot, run_id)
        except Exception:  # noqa: BLE001 — loop must survive
            log.exception("agent run %s crashed", run_id)
        finally:
            self._in_flight.discard(run_id)
