"""Shared thread helpers.

A "thread" is Telegram's message_thread_id — a forum topic or reply-thread.
Everywhere it is keyed as `thread_key = thread_id or 0` (0 == the General/main
thread == SQL thread_id IS NULL). Threads are monitored by default; an operator
can turn one off, which fully ignores it (no intent, no memory, no agent).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatThread


async def unmonitored_threads(session: AsyncSession, chat_id: int) -> set[int]:
    """The thread_keys an operator has turned off for this chat (empty = all
    monitored, the default). Cheap enough for the sweep/chunk hot paths."""
    rows = (
        await session.execute(
            select(ChatThread.thread_key).where(
                ChatThread.chat_id == chat_id, ChatThread.monitored.is_(False)
            )
        )
    ).scalars()
    return set(rows)
