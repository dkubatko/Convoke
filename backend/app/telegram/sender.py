from datetime import timezone

from aiogram import Bot as AiogramBot
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat, Message


async def send_and_persist(
    session: AsyncSession,
    bot: AiogramBot,
    chat: Chat,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
) -> TgMessage:
    """Send a message and store it as source='self'.

    Bots never receive other bots' messages via getUpdates — including their
    own — so outbound messages must be persisted at send time or the memory
    would hold one-sided conversations.
    """
    sent = await bot.send_message(
        chat.tg_chat_id,
        text,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
        allow_sending_without_reply=True,
    )
    session.add(
        Message(
            chat_id=chat.id,
            tg_message_id=sent.message_id,
            thread_id=sent.message_thread_id,
            sender_id=sent.from_user.id if sent.from_user else None,
            sender_name=sent.from_user.full_name if sent.from_user else "",
            text=sent.text or "",
            sent_at=sent.date.astimezone(timezone.utc),
            source="self",
        )
    )
    return sent
