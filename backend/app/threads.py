"""Shared thread helpers.

A "thread" is Telegram's message_thread_id — a forum topic or reply-thread.
Everywhere it is keyed as `thread_key = thread_id or 0` (0 == the General/main
thread == SQL thread_id IS NULL). Threads are monitored by default; an operator
can turn one off, which fully ignores it (no intent, no memory, no agent).
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatThread, Message


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


async def live_thread_keys(session: AsyncSession, chat_id: int) -> set[int]:
    """Thread keys that actually exist in the chat right now — derived exactly
    like the Threads tab: distinct stored-message thread_ids (main == 0) plus
    any ChatThread meta row. IntentCursor/IntentEpisode rows for keys NOT in
    this set are orphans (their messages were deleted or re-imported and never
    garbage-collected) and must not surface as phantom threads."""
    tk_col = func.coalesce(Message.thread_id, 0)
    msg_keys = (
        await session.execute(
            select(tk_col).where(Message.chat_id == chat_id).group_by(tk_col)
        )
    ).scalars()
    meta_keys = (
        await session.execute(
            select(ChatThread.thread_key).where(ChatThread.chat_id == chat_id)
        )
    ).scalars()
    return set(msg_keys) | set(meta_keys)


async def visible_thread_keys(session: AsyncSession, chat_id: int) -> set[int]:
    """Threads that should appear in workflow views: those that exist
    (`live_thread_keys`) and aren't turned off (`unmonitored_threads`)."""
    return await live_thread_keys(session, chat_id) - await unmonitored_threads(
        session, chat_id
    )
