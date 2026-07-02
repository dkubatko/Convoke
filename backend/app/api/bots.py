from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.core.db import get_session
from app.core.security import require_operator
from app.models import Bot, Chat
from app.telegram.service import InvalidBotToken, fetch_identity, register_bot

router = APIRouter(dependencies=[Depends(require_operator)])


class BotCreate(BaseModel):
    token: str


class BotOut(BaseModel):
    id: int
    tg_bot_id: int
    username: str
    name: str
    can_read_all_group_messages: bool
    status: str
    last_error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatOut(BaseModel):
    id: int
    bot_id: int
    tg_chat_id: int
    type: str
    title: str
    is_forum: bool
    status: str
    authorized_by_name: str | None
    authorized_at: datetime | None

    model_config = {"from_attributes": True}


@router.post("/bots", response_model=BotOut, status_code=status.HTTP_201_CREATED)
async def create_bot(body: BotCreate, session: AsyncSession = Depends(get_session)) -> Bot:
    try:
        bot = await register_bot(session, body.token.strip())
    except InvalidBotToken as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    await session.commit()
    return bot


@router.get("/bots", response_model=list[BotOut])
async def list_bots(session: AsyncSession = Depends(get_session)) -> list[Bot]:
    return list((await session.execute(select(Bot).order_by(Bot.id))).scalars())


@router.post("/bots/{bot_id}/recheck", response_model=BotOut)
async def recheck_bot(bot_id: int, session: AsyncSession = Depends(get_session)) -> Bot:
    """Re-read getMe — used after the operator toggles privacy mode in BotFather."""
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bot not found")
    try:
        identity = await fetch_identity(decrypt(bot.token_encrypted))
    except InvalidBotToken:
        bot.status = "error"
        bot.last_error = "Telegram rejected the token (revoked?)"
        await session.commit()
        return bot
    bot.username = identity.username
    bot.name = identity.name
    bot.can_read_all_group_messages = identity.can_read_all_group_messages
    if bot.status == "error":
        bot.status = "active"
        bot.last_error = None
    await session.commit()
    return bot


@router.delete("/bots/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bot(bot_id: int, session: AsyncSession = Depends(get_session)) -> None:
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bot not found")
    await session.delete(bot)  # chats/messages/inbox cascade
    await session.commit()


@router.get("/chats", response_model=list[ChatOut])
async def list_chats(session: AsyncSession = Depends(get_session)) -> list[Chat]:
    return list((await session.execute(select(Chat).order_by(Chat.id))).scalars())
