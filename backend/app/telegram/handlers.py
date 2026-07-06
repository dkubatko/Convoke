"""Update handlers invoked by the inbox consumer.

Each handler receives an ORM session (caller commits), the aiogram Bot for
sends, and the Bot row. Handlers must be idempotent: an update may be
re-processed after a crash between handling and the processed_at mark.
"""

import html
import logging
import re
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

from app.media.render import message_body
from app.memory.chunker import reply_quote
from app.models import AgentRun, AuthNonce, Bot, Chat, ChatThread, Message, MessageAttachment
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
    if update.message is not None and update.message.migrate_to_chat_id is not None:
        await _handle_migration(session, bot_row, update.message)
        return
    if update.my_chat_member is not None:
        await handle_my_chat_member(session, bot, bot_row, update.my_chat_member)
    elif update.callback_query is not None:
        await handle_callback_query(session, bot, bot_row, update.callback_query)
    elif update.edited_message is not None:
        await handle_edited_message(session, bot_row, update.edited_message)
    elif update.message is not None:
        await handle_message(session, bot_row, update.message)


async def _handle_migration(session: AsyncSession, bot_row: Bot, msg: TgMessage) -> None:
    """A basic group upgraded to a supergroup: Telegram issues a service
    message with migrate_to_chat_id and all future updates use the new id.
    Rewrite the existing chat's id so its stored messages, memory, and
    workflow assignments follow, instead of stranding on the dead id."""
    old_id = msg.chat.id
    new_id = msg.migrate_to_chat_id
    chat = await _get_chat(session, bot_row, old_id)
    if chat is None:
        return
    # If a chat row for the new id already exists (rare: we saw new-id updates
    # first), keep it and drop the old one to avoid a unique-constraint clash.
    existing_new = await _get_chat(session, bot_row, new_id)
    if existing_new is not None:
        await session.delete(chat)
        return
    chat.tg_chat_id = new_id
    chat.type = "supergroup"
    log.info("chat %s migrated %s -> %s", chat.id, old_id, new_id)


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
                text=f"✅ Convoke authorized by {html.escape(cb.from_user.full_name)}. "
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

    # Forum-topic service events carry the topic name — the only channel the
    # Bot API exposes it through. Capture before the no-content drop below.
    await _capture_thread_title(session, chat, msg)

    text = msg.text or msg.caption or ""
    attachment = extract_attachment(msg)
    if not text and attachment is None:
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
    message = Message(
        chat_id=chat.id,
        tg_message_id=msg.message_id,
        thread_id=msg.message_thread_id,
        reply_to_tg_message_id=(
            msg.reply_to_message.message_id if msg.reply_to_message else None
        ),
        sender_id=msg.from_user.id if msg.from_user else None,
        sender_name=msg.from_user.full_name if msg.from_user else "",
        text=text,
        sent_at=msg.date.astimezone(timezone.utc),
        source="live",
    )
    if attachment is not None:
        attachment.chat_id = chat.id
        attachment.tg_message_id = msg.message_id
    # Assign even when None: _ensure_reply_target's SELECT autoflushes this
    # pending row, and message_body reads .attachment right after — an
    # uninitialized selectin relationship would lazy-load, illegal in async.
    message.attachment = attachment  # cascades on session.add(message)
    session.add(message)

    reply_target = (
        await _ensure_reply_target(session, chat, msg.reply_to_message)
        if msg.reply_to_message is not None
        else None
    )

    trigger = _agent_trigger(msg, bot_row, text)
    # An unmonitored thread is fully ignored: no proactive workflows, no memory,
    # and no agent replies — not even to a direct @mention or reply.
    if trigger is not None and await _thread_monitored(session, chat.id, msg.message_thread_id):
        request_text = message_body(message)
        # The trigger often only makes sense with its reply target ("what does
        # the message I replied to say?") — carry the quote so the semantic
        # query and the run log are self-contained.
        if reply_target is not None and message_body(reply_target):
            request_text += "\n" + reply_quote(reply_target)
        session.add(
            AgentRun(
                chat_id=chat.id,
                trigger=trigger,
                trigger_tg_message_id=msg.message_id,
                thread_id=msg.message_thread_id,
                request_text=request_text,
            )
        )


async def _capture_thread_title(session: AsyncSession, chat: Chat, msg: TgMessage) -> None:
    """Record a forum topic's name from its create/edit service event — the only
    way the Bot API surfaces it. Existing topics (created before the bot could
    see the event) get a default name in the UI and can be renamed by hand."""
    event = msg.forum_topic_created or msg.forum_topic_edited
    name = getattr(event, "name", None) if event is not None else None
    if not name:
        return
    thread_key = msg.message_thread_id or msg.message_id
    row = await session.get(ChatThread, (chat.id, thread_key))
    if row is None:
        session.add(ChatThread(chat_id=chat.id, thread_key=thread_key, title=name))
    else:
        row.title = name


async def _thread_monitored(session: AsyncSession, chat_id: int, thread_id: int | None) -> bool:
    """A thread is monitored unless an operator turned it off (no row = default
    on). thread_id None is the General thread (thread_key 0)."""
    row = await session.get(ChatThread, (chat_id, thread_id or 0))
    return row is None or row.monitored


async def _ensure_reply_target(
    session: AsyncSession, chat: Chat, target: TgMessage
) -> Message | None:
    """The replied-to message as a stored row. Telegram inlines the full
    original in reply_to_message, so a target Convoke never saw (sent before
    authorization, or during an offline gap) is persisted now — reply context
    must not depend on the bot having been online when the original arrived."""
    row = (
        await session.execute(
            select(Message).where(
                Message.chat_id == chat.id, Message.tg_message_id == target.message_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    text = target.text or target.caption or ""
    attachment = extract_attachment(target)
    if not text and attachment is None:
        return None  # service messages (topic created, pins…) aren't content
    row = Message(
        chat_id=chat.id,
        tg_message_id=target.message_id,
        thread_id=target.message_thread_id,
        reply_to_tg_message_id=(
            target.reply_to_message.message_id if target.reply_to_message else None
        ),
        sender_id=target.from_user.id if target.from_user else None,
        sender_name=target.from_user.full_name if target.from_user else "",
        text=text,
        sent_at=target.date.astimezone(timezone.utc),
        source="live",
    )
    if attachment is not None:
        attachment.chat_id = chat.id
        attachment.tg_message_id = target.message_id
    # Always assign (even None): message_body reads .attachment on this still-
    # pending row, and an uninitialized selectin relationship would lazy-load —
    # illegal in async context.
    row.attachment = attachment
    session.add(row)
    return row


def extract_attachment(msg: TgMessage) -> MessageAttachment | None:
    """Map the message's media (if any) to an attachment row. chat_id and
    tg_message_id are filled in by the caller. Telegram messages carry at most
    one media item; album members arrive as separate messages sharing
    media_group_id."""
    if msg.photo:
        p = msg.photo[-1]  # renditions are sorted ascending; keep the largest
        return MessageAttachment(
            kind="photo",
            file_id=p.file_id,
            file_unique_id=p.file_unique_id,
            size_bytes=p.file_size,
            width=p.width,
            height=p.height,
            media_group_id=msg.media_group_id,
        )
    if msg.video:
        v = msg.video
        return MessageAttachment(
            kind="video",
            file_id=v.file_id,
            file_unique_id=v.file_unique_id,
            mime=v.mime_type,
            size_bytes=v.file_size,
            width=v.width,
            height=v.height,
            duration_s=v.duration,
            thumb_file_id=v.thumbnail.file_id if v.thumbnail else None,
            media_group_id=msg.media_group_id,
        )
    if msg.voice:
        return MessageAttachment(
            kind="voice",
            file_id=msg.voice.file_id,
            file_unique_id=msg.voice.file_unique_id,
            mime=msg.voice.mime_type,
            size_bytes=msg.voice.file_size,
            duration_s=msg.voice.duration,
        )
    if msg.video_note:
        vn = msg.video_note
        return MessageAttachment(
            kind="video_note",
            file_id=vn.file_id,
            file_unique_id=vn.file_unique_id,
            size_bytes=vn.file_size,
            duration_s=vn.duration,
            thumb_file_id=vn.thumbnail.file_id if vn.thumbnail else None,
        )
    if msg.sticker:
        s = msg.sticker
        return MessageAttachment(
            kind="sticker",
            file_id=s.file_id,
            file_unique_id=s.file_unique_id,
            size_bytes=s.file_size,
            width=s.width,
            height=s.height,
            sticker_emoji=s.emoji,
            thumb_file_id=s.thumbnail.file_id if s.thumbnail else None,
        )
    if msg.animation:  # GIFs; msg.document duplicates this, so check first
        a = msg.animation
        return MessageAttachment(
            kind="video",
            file_id=a.file_id,
            file_unique_id=a.file_unique_id,
            mime=a.mime_type,
            size_bytes=a.file_size,
            width=a.width,
            height=a.height,
            duration_s=a.duration,
            thumb_file_id=a.thumbnail.file_id if a.thumbnail else None,
        )
    if msg.audio:
        return MessageAttachment(
            kind="audio",
            file_id=msg.audio.file_id,
            file_unique_id=msg.audio.file_unique_id,
            mime=msg.audio.mime_type,
            size_bytes=msg.audio.file_size,
            duration_s=msg.audio.duration,
        )
    if msg.document and (msg.document.mime_type or "").startswith("image/"):
        d = msg.document
        return MessageAttachment(
            kind="image_document",
            file_id=d.file_id,
            file_unique_id=d.file_unique_id,
            mime=d.mime_type,
            size_bytes=d.file_size,
            thumb_file_id=d.thumbnail.file_id if d.thumbnail else None,
        )
    return None  # non-image documents, polls, locations… stay out of scope


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
    # Word-boundary match so "@mybot" doesn't fire on "@mybotnews". Telegram
    # usernames are [A-Za-z0-9_], so a trailing such char means a longer name.
    if bot_row.username:
        handle = f"@{bot_row.username.lower()}"
        for m in re.finditer(re.escape(handle), text.lower()):
            after = text[m.end() : m.end() + 1]
            if not (after.isalnum() or after == "_"):
                return "mention"
    return None


async def handle_edited_message(session: AsyncSession, bot_row: Bot, msg: TgMessage) -> None:
    if msg.chat.type not in GROUP_TYPES:
        return
    chat = await _get_chat(session, bot_row, msg.chat.id)
    if chat is None or chat.status != "authorized":
        return
    text = msg.text or msg.caption or ""
    if not text and extract_attachment(msg) is None:
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
