"""Per-bot long-polling with persist-then-ack.

Telegram's getUpdates offset is the only redelivery mechanism we get: an
update is gone the moment we poll with a higher offset. So the polling loop
does exactly one thing — write raw updates to the inbox table, commit, and
only then advance the offset. Everything else happens in DB-driven consumers.
"""

import asyncio
import logging

from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import decrypt
from app.models import Bot, InboxUpdate
from app.telegram.client import ALLOWED_UPDATES, make_bot

log = logging.getLogger("convoke.gateway")

POLL_TIMEOUT_S = 30
RECONCILE_INTERVAL_S = 5
ERROR_BACKOFF_S = 5


class BotRunner:
    def __init__(self, bot_id: int, token: str, next_offset: int,
                 sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.bot_id = bot_id
        self.next_offset = next_offset
        self.sessionmaker = sessionmaker
        self.bot = make_bot(token)

    async def run(self) -> None:
        log.info("bot %s: polling started (offset=%s)", self.bot_id, self.next_offset)
        try:
            while True:
                await self._poll_once()
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError:
            log.error("bot %s: token revoked; disabling", self.bot_id)
            await self._mark_error("Telegram rejected the token (revoked?)")
        finally:
            await self.bot.session.close()

    async def _poll_once(self) -> None:
        try:
            updates = await self.bot.get_updates(
                offset=self.next_offset or None,
                timeout=POLL_TIMEOUT_S,
                allowed_updates=ALLOWED_UPDATES,
                request_timeout=POLL_TIMEOUT_S + 15,
            )
        except TelegramRetryAfter as e:
            log.warning("bot %s: rate limited for %ss", self.bot_id, e.retry_after)
            await asyncio.sleep(e.retry_after)
            return
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as e:
            log.warning("bot %s: poll error (%s); backing off", self.bot_id, e)
            await asyncio.sleep(ERROR_BACKOFF_S)
            return
        if not updates:
            return

        new_offset = max(u.update_id for u in updates) + 1
        async with self.sessionmaker() as session:
            for u in updates:
                await session.execute(
                    pg_insert(InboxUpdate)
                    .values(
                        bot_id=self.bot_id,
                        update_id=u.update_id,
                        payload=u.model_dump(mode="json", exclude_none=True),
                    )
                    .on_conflict_do_nothing(index_elements=["bot_id", "update_id"])
                )
            await session.execute(
                update(Bot).where(Bot.id == self.bot_id).values(next_offset=new_offset)
            )
            await session.commit()
        # Persisted and committed — safe to ack on the next getUpdates call.
        self.next_offset = new_offset

    async def _mark_error(self, message: str) -> None:
        async with self.sessionmaker() as session:
            await session.execute(
                update(Bot).where(Bot.id == self.bot_id).values(status="error", last_error=message)
            )
            await session.commit()


class Gateway:
    """Reconciles polling tasks against the bots table so bots added or
    removed via the UI (a different process) take effect within seconds."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker
        self.tasks: dict[int, asyncio.Task] = {}

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self._reconcile()
                except Exception:  # noqa: BLE001 — reconcile must survive transient failures
                    log.exception("gateway reconcile failed")
                await asyncio.sleep(RECONCILE_INTERVAL_S)
        finally:
            for task in self.tasks.values():
                task.cancel()

    async def _reconcile(self) -> None:
        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Bot.id, Bot.token_encrypted, Bot.next_offset).where(
                        Bot.status == "active"
                    )
                )
            ).all()
        active = {row.id: row for row in rows}

        for bot_id in list(self.tasks):
            if bot_id not in active or self.tasks[bot_id].done():
                self.tasks.pop(bot_id).cancel()
                log.info("bot %s: polling stopped", bot_id)

        for bot_id, row in active.items():
            if bot_id not in self.tasks:
                try:
                    token = decrypt(row.token_encrypted)
                except Exception:  # noqa: BLE001 — e.g. Fernet key changed
                    log.exception("cannot decrypt token for bot %s; not polling it", bot_id)
                    continue
                runner = BotRunner(bot_id, token, row.next_offset, self.sessionmaker)
                self.tasks[bot_id] = asyncio.create_task(
                    runner.run(), name=f"bot-poll-{bot_id}"
                )
