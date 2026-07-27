from datetime import datetime, timezone

from aiogram import Bot as AiogramBot
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat, Message
from app.telegram.media import OutgoingMedia, extract_attachment


async def send_and_persist(
    session: AsyncSession,
    bot: AiogramBot,
    chat: Chat,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
    thread_id: int | None = None,
) -> TgMessage:
    """Send a message and store it as source='self'.

    Bots never receive other bots' messages via getUpdates — including their
    own — so outbound messages must be persisted at send time or the memory
    would hold one-sided conversations.

    thread_id routes the message to the right forum topic; without it,
    workflow confirmations and workflow-run replies (which have no reply_to)
    land in the supergroup's General topic instead of where the intent arose.
    """
    sent = await bot.send_message(
        chat.tg_chat_id,
        text,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=thread_id,
        allow_sending_without_reply=True,
    )
    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=sent.message_id,
            thread_id=sent.message_thread_id,
            # From `sent`, not the argument: with allow_sending_without_reply
            # Telegram may have dropped the link (target deleted meanwhile).
            reply_to_tg_message_id=(
                sent.reply_to_message.message_id if sent.reply_to_message else None
            ),
            sender_id=sent.from_user.id if sent.from_user else None,
            sender_name=sent.from_user.full_name if sent.from_user else "",
            text=sent.text or "",
            sent_at=sent.date.astimezone(timezone.utc),
            source="self",
        )
    )
    return sent


async def send_media_and_persist(
    session: AsyncSession,
    bot: AiogramBot,
    chat: Chat,
    items: list[OutgoingMedia],
    thread_id: int | None = None,
) -> list[TgMessage]:
    """Send the agent's attached media as ONE Telegram send — a single
    photo/video, or one album (2–10 mixed) — and persist each resulting
    message as source='self' with its attachment, so the bot's own media
    lands in memory like any incoming media.

    Media never carries a reply link: the reply text does the pointing;
    media follows it in the same thread. No caption either (v1). Raising is
    the caller's failure signal — nothing is persisted unless the send
    succeeded, and sendMediaGroup is all-or-nothing on Telegram's side.

    History items arrive with the source attachment's description copied and
    are persisted already `described` (MediaLoop only scans `pending`, so a
    re-sent photo is never re-described). URL items stay `pending` and get
    described once via the normal pipeline."""
    if not items:
        return []
    if len(items) == 1:
        item = items[0]
        ref = item.file_id or item.url
        if item.kind == "video":
            sent_list = [
                await bot.send_video(chat.tg_chat_id, video=ref, message_thread_id=thread_id)
            ]
        else:
            sent_list = [
                await bot.send_photo(chat.tg_chat_id, photo=ref, message_thread_id=thread_id)
            ]
    else:
        group = [
            InputMediaVideo(media=i.file_id or i.url)
            if i.kind == "video"
            else InputMediaPhoto(media=i.file_id or i.url)
            for i in items
        ]
        # Returns the sent messages in input order — zip below relies on it.
        sent_list = await bot.send_media_group(
            chat.tg_chat_id, media=group, message_thread_id=thread_id
        )
    for item, sent in zip(items, sent_list):
        att = extract_attachment(sent)
        if att is not None:
            att.chat_id = chat.id
            att.tg_message_id = sent.message_id
            if item.described:
                att.description = item.description
                att.transcript = item.transcript
                att.status = "described"
                att.described_at = datetime.now(timezone.utc)
        row = Message(
            chat_id=chat.id,
            tg_message_id=sent.message_id,
            thread_id=sent.message_thread_id,
            sender_id=sent.from_user.id if sent.from_user else None,
            sender_name=sent.from_user.full_name if sent.from_user else "",
            text=sent.caption or "",
            sent_at=sent.date.astimezone(timezone.utc),
            source="self",
        )
        # Always assign (even None): message_body reads .attachment on this
        # still-pending row, and an uninitialized selectin relationship would
        # lazy-load — illegal in async context.
        row.attachment = att
        session.add(row)
    return sent_list
