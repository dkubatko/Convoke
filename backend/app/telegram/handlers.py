"""Update handlers invoked by the inbox consumer.

Each handler receives an ORM session (caller commits), the aiogram Bot for
sends, and the Bot row. Handlers must be idempotent: an update may be
re-processed after a crash between handling and the processed_at mark.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from aiogram import Bot as AiogramBot
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from aiogram.types import Message as TgMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, AuthNonce, Bot, Chat, Message
from app.telegram.sender import send_and_persist

log = logging.getLogger("convoke.telegram")

GROUP_TYPES = ("group", "supergroup")
JOINED_STATUSES = ("member", "administrator", "restricted")
GONE_STATUSES = ("left", "kicked")
AUTH_NONCE_TTL = timedelta(hours=24)
AUTH_CALLBACK_PREFIX = "auth:"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    # SQLite (tests) hands back naive datetimes where Postgres returns aware.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def handle_update(session: AsyncSession, bot: AiogramBot, bot_row: Bot, update: Update) -> None:
    if update.my_chat_member is not None:
        await handle_my_chat_member(session, bot, bot_row, update.my_chat_member)
    elif update.callback_query is not None:
        await handle_callback_query(session, bot, bot_row, update.callback_query)
    elif update.edited_message is not None:
        await handle_edited_message(session, bot_row, update.edited_message)
    elif update.message is not None:
        await handle_message(session, bot_row, update.message)


async def _get_chat(session: AsyncSession, bot_row: Bot, tg_chat_id: int) -> Chat | None:
    return (
        await session.execute(
            select(Chat).where(Chat.bot_id == bot_row.id, Chat.tg_chat_id == tg_chat_id)
        )
    ).scalar_one_or_none()


async def handle_my_chat_member(
    session: AsyncSession, bot: AiogramBot, bot_row: Bot, event: ChatMemberUpdated
) -> None:
    if event.chat.type not in GROUP_TYPES:
        return
    new_status = event.new_chat_member.status
    chat = await _get_chat(session, bot_row, event.chat.id)

    if new_status in GONE_STATUSES:
        if chat is not None:
            chat.status = "left"
        return

    if new_status not in JOINED_STATUSES:
        return

    if chat is None:
        chat = Chat(
            bot_id=bot_row.id,
            tg_chat_id=event.chat.id,
            type=event.chat.type,
            title=event.chat.title or "",
            is_forum=bool(event.chat.is_forum),
        )
        session.add(chat)
        await session.flush()
    else:
        chat.title = event.chat.title or chat.title
        chat.is_forum = bool(event.chat.is_forum)
        if chat.status == "left":
            # Re-added after leaving: prior authorization does not carry over.
            chat.status = "pending_auth"

    if chat.status != "pending_auth":
        return  # already authorized; promotion to admin etc. needs no prompt

    await _send_auth_prompt(session, bot, chat)


async def _send_auth_prompt(session: AsyncSession, bot: AiogramBot, chat: Chat) -> None:
    nonce = secrets.token_urlsafe(16)
    nonce_row = AuthNonce(nonce=nonce, chat_id=chat.id, expires_at=_utcnow() + AUTH_NONCE_TTL)
    session.add(nonce_row)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Authorize Convoke", callback_data=f"{AUTH_CALLBACK_PREFIX}{nonce}"
                )
            ]
        ]
    )
    sent = await send_and_persist(
        session,
        bot,
        chat,
        "👋 Convoke was added to this chat. Once a <b>chat admin</b> authorizes it, "
        "messages here will be stored as the assistant's memory for this chat.",
        reply_markup=markup,
    )
    nonce_row.tg_message_id = sent.message_id


async def handle_callback_query(
    session: AsyncSession, bot: AiogramBot, bot_row: Bot, cb: CallbackQuery
) -> None:
    data = cb.data or ""
    if data.startswith("wf:"):
        from app.intent.executor import handle_confirm_callback

        await handle_confirm_callback(session, bot, data, cb.from_user.full_name, cb.id)
        return
    if not data.startswith(AUTH_CALLBACK_PREFIX):
        return
    nonce = data[len(AUTH_CALLBACK_PREFIX) :]
    nonce_row = (
        await session.execute(select(AuthNonce).where(AuthNonce.nonce == nonce))
    ).scalar_one_or_none()
    if nonce_row is None or nonce_row.used_at is not None or _as_utc(nonce_row.expires_at) < _utcnow():
        await bot.answer_callback_query(
            cb.id, text="This authorization request has expired.", show_alert=True
        )
        return
    chat = await session.get(Chat, nonce_row.chat_id)
    if chat is None or chat.bot_id != bot_row.id:
        await bot.answer_callback_query(cb.id, text="Unknown chat.", show_alert=True)
        return

    # Authority check happens at click time — never trust callback_data,
    # and never trust admin status cached from earlier.
    member = await bot.get_chat_member(chat.tg_chat_id, cb.from_user.id)
    if member.status not in ("creator", "administrator"):
        await bot.answer_callback_query(
            cb.id, text="Only chat admins can authorize Convoke.", show_alert=True
        )
        return

    chat.status = "authorized"
    chat.authorized_by_user_id = cb.from_user.id
    chat.authorized_by_name = cb.from_user.full_name
    chat.authorized_at = _utcnow()
    nonce_row.used_at = _utcnow()

    await bot.answer_callback_query(cb.id, text="Convoke authorized ✅")
    if nonce_row.tg_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat.tg_chat_id,
                message_id=nonce_row.tg_message_id,
                text=f"✅ Convoke authorized by {cb.from_user.full_name}. "
                "This chat's messages are now part of the assistant's memory.",
            )
        except Exception:  # noqa: BLE001 — cosmetic edit; authorization already stands
            log.warning("could not edit auth prompt in chat %s", chat.tg_chat_id)


async def handle_message(session: AsyncSession, bot_row: Bot, msg: TgMessage) -> None:
    if msg.chat.type not in GROUP_TYPES:
        return
    chat = await _get_chat(session, bot_row, msg.chat.id)
    if chat is None or chat.status != "authorized":
        return  # consent-first: nothing is stored before an admin authorizes
    if msg.chat.title and msg.chat.title != chat.title:
        chat.title = msg.chat.title

    text = msg.text or msg.caption
    if not text:
        return

    existing = (
        await session.execute(
            select(Message).where(
                Message.chat_id == chat.id, Message.tg_message_id == msg.message_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return  # idempotent re-processing
    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=msg.message_id,
            thread_id=msg.message_thread_id,
            sender_id=msg.from_user.id if msg.from_user else None,
            sender_name=msg.from_user.full_name if msg.from_user else "",
            text=text,
            sent_at=msg.date.astimezone(timezone.utc),
            source="live",
        )
    )

    trigger = _agent_trigger(msg, bot_row, text)
    if trigger is not None:
        session.add(
            AgentRun(
                chat_id=chat.id,
                trigger=trigger,
                trigger_tg_message_id=msg.message_id,
                thread_id=msg.message_thread_id,
                request_text=text,
            )
        )


def _agent_trigger(msg: TgMessage, bot_row: Bot, text: str) -> str | None:
    """A message invokes the agent when it @mentions the bot or replies to
    one of the bot's messages."""
    reply_to = msg.reply_to_message
    if (
        reply_to is not None
        and reply_to.from_user is not None
        and reply_to.from_user.id == bot_row.tg_bot_id
    ):
        return "reply"
    if bot_row.username and f"@{bot_row.username.lower()}" in text.lower():
        return "mention"
    return None


async def handle_edited_message(session: AsyncSession, bot_row: Bot, msg: TgMessage) -> None:
    if msg.chat.type not in GROUP_TYPES:
        return
    chat = await _get_chat(session, bot_row, msg.chat.id)
    if chat is None or chat.status != "authorized":
        return
    text = msg.text or msg.caption
    if not text:
        return
    existing = (
        await session.execute(
            select(Message).where(
                Message.chat_id == chat.id, Message.tg_message_id == msg.message_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        # Edit of a message we never saw (sent before authorization) — store it now.
        await handle_message(session, bot_row, msg)
        return
    from app.memory.store import mark_chunks_stale

    existing.text = text
    await mark_chunks_stale(session, chat.id, msg.message_id)
    existing.edited_at = (
        msg.edit_date.astimezone(timezone.utc)
        if isinstance(msg.edit_date, datetime)
        else datetime.fromtimestamp(msg.edit_date, tz=timezone.utc)
        if msg.edit_date
        else _utcnow()
    )
