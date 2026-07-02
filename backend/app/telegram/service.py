from dataclasses import dataclass

from aiogram.exceptions import TelegramUnauthorizedError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt
from app.models import Bot
from app.telegram.client import make_bot


class InvalidBotToken(ValueError):
    pass


@dataclass
class BotIdentity:
    tg_bot_id: int
    username: str
    name: str
    can_read_all_group_messages: bool


async def fetch_identity(token: str) -> BotIdentity:
    """Validate a token against Telegram and read the bot's privacy-mode state."""
    bot = make_bot(token)
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        raise InvalidBotToken("Telegram rejected this token")
    finally:
        await bot.session.close()
    return BotIdentity(
        tg_bot_id=me.id,
        username=me.username or "",
        name=me.full_name,
        can_read_all_group_messages=bool(me.can_read_all_group_messages),
    )


async def register_bot(session: AsyncSession, token: str) -> Bot:
    identity = await fetch_identity(token)
    existing = (
        await session.execute(select(Bot).where(Bot.tg_bot_id == identity.tg_bot_id))
    ).scalar_one_or_none()
    if existing is not None:
        # Re-registering refreshes the token and identity (e.g. after /revoke).
        existing.token_encrypted = encrypt(token)
        existing.username = identity.username
        existing.name = identity.name
        existing.can_read_all_group_messages = identity.can_read_all_group_messages
        existing.status = "active"
        existing.last_error = None
        return existing
    bot = Bot(
        tg_bot_id=identity.tg_bot_id,
        username=identity.username,
        name=identity.name,
        token_encrypted=encrypt(token),
        can_read_all_group_messages=identity.can_read_all_group_messages,
    )
    session.add(bot)
    await session.flush()
    return bot
