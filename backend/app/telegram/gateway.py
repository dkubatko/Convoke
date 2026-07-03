"""Per-bot long-polling with persist-then-ack.

Telegram's getUpdates offset is the only redelivery mechanism we get: an
update is gone the moment we poll with a higher offset. So the polling loop
does exactly one thing — write raw updates to the inbox table, commit, and
only then advance the offset. Everything else happens in DB-driven consumers.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

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
from app.models import Bot, Chat, InboxUpdate, MemoryGap
from app.telegram.client import ALLOWED_UPDATES, make_bot

log = logging.getLogger("convoke.gateway")

POLL_TIMEOUT_S = 30
RECONCILE_INTERVAL_S = 5
ERROR_BACKOFF_S = 5
POLL_STAMP_INTERVAL_S = 60
# Telegram retains unconfirmed updates for 24h; longer downtime = data loss.
GAP_THRESHOLD = timedelta(hours=24)


def serialize_update(u) -> dict:
    """Raw update → JSON for the inbox.

    exclude_unset is load-bearing: aiogram fills unset fields with Default
    sentinels that pydantic cannot serialize (a message carrying link-preview
    data once wedged a bot in a silent crash loop this way). Storing only the
    fields Telegram actually sent avoids the sentinels entirely. As a last
    resort a single unserializable update degrades to an error stub rather
    than blocking the offset forever.
    """
    try:
        return u.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    except Exception as e:  # noqa: BLE001 — one poisoned update must not stop the wire
        log.exception("update %s could not be serialized; storing error stub", u.update_id)
        return {"update_id": u.update_id, "convoke_serialize_error": f"{type(e).__name__}: {e}"}


class BotRunner:
    def __init__(self, bot_id: int, token: str, next_offset: int,
                 sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.bot_id = bot_id
        self.next_offset = next_offset
        self.sessionmaker = sessionmaker
        self.bot = make_bot(token)
        self._last_stamp: datetime | None = None

    async def run(self) -> None:
        log.info("bot %s: polling started (offset=%s)", self.bot_id, self.next_offset)
        try:
            await self._mark_gap_if_stale()
            while True:
                try:
                    await self._poll_once()
                except (asyncio.CancelledError, TelegramUnauthorizedError):
                    raise
                except Exception:  # noqa: BLE001 — a crash loop must be LOUD, never silent
                    log.exception(
                        "bot %s: poll cycle failed at offset %s; retrying in %ss",
                        self.bot_id, self.next_offset, ERROR_BACKOFF_S,
                    )
                    await asyncio.sleep(ERROR_BACKOFF_S)
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
        await self._stamp_polled()
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
                        payload=serialize_update(u),
                    )
                    .on_conflict_do_nothing(index_elements=["bot_id", "update_id"])
                )
            await session.execute(
                update(Bot).where(Bot.id == self.bot_id).values(next_offset=new_offset)
            )
            await session.commit()
        # Persisted and committed — safe to ack on the next getUpdates call.
        self.next_offset = new_offset

    async def _stamp_polled(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_stamp is not None and (now - self._last_stamp).total_seconds() < POLL_STAMP_INTERVAL_S:
            return
        self._last_stamp = now
        async with self.sessionmaker() as session:
            await session.execute(
                update(Bot).where(Bot.id == self.bot_id).values(last_polled_at=now)
            )
            await session.commit()

    async def _mark_gap_if_stale(self) -> None:
        """Downtime past Telegram's 24h retention = messages permanently lost.
        Record the hole so the UI shows it and agent context mentions it."""
        now = datetime.now(timezone.utc)
        async with self.sessionmaker() as session:
            last = (
                await session.execute(select(Bot.last_polled_at).where(Bot.id == self.bot_id))
            ).scalar_one_or_none()
            if last is None:
                return
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last <= GAP_THRESHOLD:
                return
            chat_ids = (
                await session.execute(
                    select(Chat.id).where(Chat.bot_id == self.bot_id, Chat.status == "authorized")
                )
            ).scalars().all()
            for chat_id in chat_ids:
                # A crash-restart loop while offline must not write the same
                # gap once per restart: skip if this gap_start is recorded.
                existing = (
                    await session.execute(
                        select(MemoryGap.id).where(
                            MemoryGap.chat_id == chat_id, MemoryGap.gap_start == last
                        )
                    )
                ).first()
                if existing is None:
                    session.add(MemoryGap(chat_id=chat_id, gap_start=last, gap_end=now))
            await session.commit()
            if chat_ids:
                log.warning(
                    "bot %s: offline since %s (>24h) — marked memory gaps in %d chat(s)",
                    self.bot_id, last.isoformat(), len(chat_ids),
                )

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
                task = self.tasks.pop(bot_id)
                if task.done() and not task.cancelled() and task.exception() is not None:
                    log.error(
                        "bot %s: polling task died: %r", bot_id, task.exception()
                    )
                task.cancel()
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
