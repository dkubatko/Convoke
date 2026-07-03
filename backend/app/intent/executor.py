"""PendingFire executor: the confirm gate and the actual firing.

pending → (confirm workflow?) confirm_wait → confirmed → done
        └──────────────── otherwise ────────┘

Firing = creating an AgentRun (the agent loop executes it with the chat's
MCP tools). AgentRun creation and marking the fire done share one commit, so
a crash can't double-fire.
"""

import asyncio
import html
import logging
import secrets
from datetime import datetime, timedelta, timezone

from aiogram import Bot as AiogramBot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.crypto import decrypt
from app.intent.state import render_slots
from app.models import AgentRun, Bot, Chat, PendingFire, Workflow
from app.telegram.client import BotCache
from app.telegram.limiter import SendLimiter
from app.telegram.sender import send_and_persist

log = logging.getLogger("convoke.fires")

POLL_S = 2.0
CONFIRM_CALLBACK_PREFIX = "wf:"


def build_action_request(wf: Workflow, slots: dict) -> str:
    text = wf.action_prompt
    if slots:
        text += "\n\nInformation gathered from the conversation:\n" + render_slots(slots)
    return text


class FireExecutor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        limiter: SendLimiter,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.limiter = limiter
        self._bots = BotCache()

    async def _bot_for(self, session: AsyncSession, bot_id: int) -> AiogramBot:
        bot_row = await session.get(Bot, bot_id)
        return self._bots.get(bot_id, bot_row.token_encrypted, decrypt(bot_row.token_encrypted))

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001 — loop must survive
                    log.exception("fire executor tick failed")
                await asyncio.sleep(POLL_S)
        finally:
            await self._bots.aclose()

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.sessionmaker() as session:
            # Iterate ids, re-fetching each fire fresh: a per-fire rollback
            # (below) expires the whole identity map, so a pre-loaded ORM list
            # would lazy-load — and hit IO — on the next iteration's attribute.
            fire_ids = (
                (
                    await session.execute(
                        select(PendingFire.id)
                        .where(PendingFire.status.in_(("pending", "confirmed")))
                        .order_by(PendingFire.id)
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for fire_id in fire_ids:
                fire = await session.get(PendingFire, fire_id)
                if fire is None:
                    continue
                wf = await session.get(Workflow, fire.workflow_id)
                chat = await session.get(Chat, fire.chat_id)
                if wf is None or chat is None:
                    fire.status = "cancelled"
                    fire.finished_at = now
                    await session.commit()
                    continue
                # Each fire is its own transaction: a bad send (bot kicked,
                # parse error) fails ONLY that fire and can't roll back a fire
                # already confirmed this tick, nor re-send its confirmation.
                try:
                    if fire.status == "pending" and wf.confirm:
                        await self._request_confirmation(session, fire, wf, chat)
                    else:
                        await self._fire(session, fire, wf)
                    await session.commit()
                except Exception as e:  # noqa: BLE001 — poison fire must not wedge the queue
                    await session.rollback()
                    log.exception("fire %s failed", fire_id)
                    fresh = await session.get(PendingFire, fire_id)
                    if fresh is not None:
                        fresh.status = "error"
                        fresh.error = f"{type(e).__name__}: {e}"[:300]
                        fresh.finished_at = datetime.now(timezone.utc)
                        await session.commit()
            await self._expire_stale_confirmations(session, now)
            await session.commit()

    async def _fire(self, session: AsyncSession, fire: PendingFire, wf: Workflow) -> None:
        run = AgentRun(
            chat_id=fire.chat_id,
            trigger="workflow",
            workflow_id=wf.id,
            thread_id=fire.thread_key or None,
            request_text=build_action_request(wf, fire.slots or {}),
        )
        session.add(run)
        await session.flush()
        fire.status = "done"
        fire.finished_at = datetime.now(timezone.utc)
        fire.agent_run_id = run.id
        log.info("workflow %s fired in chat %s (run %s queued)", wf.id, fire.chat_id, run.id)

    async def _request_confirmation(
        self, session: AsyncSession, fire: PendingFire, wf: Workflow, chat: Chat
    ) -> None:
        nonce = secrets.token_urlsafe(12)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Go ahead", callback_data=f"{CONFIRM_CALLBACK_PREFIX}y:{nonce}"),
                    InlineKeyboardButton(text="❌ Cancel", callback_data=f"{CONFIRM_CALLBACK_PREFIX}n:{nonce}"),
                ]
            ]
        )
        bot = await self._bot_for(session, chat.bot_id)
        await self.limiter.acquire(chat.bot_id, chat.tg_chat_id)
        # HTML parse mode + slot values lifted verbatim from members' messages:
        # escape both or a value like "<cafe>" fails to parse (wedging the
        # fire) and "<b>…" would spoof formatting in the bot's official prompt.
        slots_text = html.escape(render_slots(fire.slots or {}))
        sent = await send_and_persist(
            session,
            bot,
            chat,
            f"🤖 <b>{html.escape(wf.name)}</b> is ready to act:\n"
            f"<pre>{slots_text}</pre>\n"
            "Anyone can confirm or cancel.",
            reply_markup=markup,
            thread_id=fire.thread_key or None,
        )
        fire.status = "confirm_wait"
        fire.confirm_nonce = nonce
        fire.confirm_tg_message_id = sent.message_id

    async def _expire_stale_confirmations(self, session: AsyncSession, now: datetime) -> None:
        timeout = timedelta(minutes=get_settings().confirm_timeout_minutes)
        stale = (
            (
                await session.execute(
                    select(PendingFire).where(PendingFire.status == "confirm_wait")
                )
            )
            .scalars()
            .all()
        )
        for fire in stale:
            created = fire.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if now - created > timeout:
                fire.status = "cancelled"
                fire.error = "confirmation timed out"
                fire.finished_at = now


async def handle_confirm_callback(
    session: AsyncSession, bot: AiogramBot, data: str, from_user_name: str, cb_id: str
) -> None:
    """Invoked by the inbox consumer for wf:* callback queries."""
    try:
        decision, nonce = data[len(CONFIRM_CALLBACK_PREFIX) :].split(":", 1)
    except ValueError:
        return
    fire = (
        await session.execute(select(PendingFire).where(PendingFire.confirm_nonce == nonce))
    ).scalar_one_or_none()
    if fire is None or fire.status != "confirm_wait":
        await bot.answer_callback_query(cb_id, text="This request is no longer active.")
        return
    chat = await session.get(Chat, fire.chat_id)
    if decision == "y":
        fire.status = "confirmed"
        await bot.answer_callback_query(cb_id, text="Confirmed ✅")
        outcome = f"✅ Confirmed by {html.escape(from_user_name)}."
    else:
        fire.status = "cancelled"
        fire.finished_at = datetime.now(timezone.utc)
        await bot.answer_callback_query(cb_id, text="Cancelled")
        outcome = f"❌ Cancelled by {html.escape(from_user_name)}."
    if fire.confirm_tg_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat.tg_chat_id, message_id=fire.confirm_tg_message_id, text=outcome
            )
        except Exception:  # noqa: BLE001 — cosmetic
            log.warning("could not edit confirmation message in chat %s", chat.tg_chat_id)
