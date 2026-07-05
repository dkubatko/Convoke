"""Segmentation of chat messages into embeddable conversation chunks.

Segments close on a conversation lull or at max size, per thread (forum
supergroups interleave unrelated topics). A trailing overlap from the previous
segment keeps context across boundaries. The active (unclosed) tail of a chat
is never chunked — recent messages always enter agent context verbatim.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.media.render import message_body
from app.models import Chunk, ChunkState, Message


@dataclass
class Segment:
    thread_id: int | None
    messages: list[Message]  # includes overlap prefix
    tg_id_start: int  # excludes overlap: cursor range actually covered
    tg_id_end: int


def render_message(m: Message) -> str:
    ts = m.sent_at.strftime("%Y-%m-%d %H:%M")
    return f"{m.sender_name or 'Unknown'} [{ts}]: {message_body(m)}"


def render_segment(seg: Segment, reply_targets: dict[int, Message] | None = None) -> str:
    """Render a segment; a reply whose target is OUTSIDE the segment gets the
    quoted original appended (in-segment targets are visible lines already —
    never duplicated). This bakes reply context into the chunk text, so both
    its embedding and search_chat_history hits carry it."""
    present = {m.tg_message_id for m in seg.messages}
    lines: list[str] = []
    for m in seg.messages:
        line = render_message(m)
        rid = m.reply_to_tg_message_id
        if rid and rid not in present:
            target = (reply_targets or {}).get(rid)
            if target is not None and message_body(target):
                q = message_body(target).replace("\n", " ")
                if len(q) > 120:
                    q = q[:120] + "…"
                line += f'\n  ↳ (replies to {target.sender_name or "Unknown"}: "{q}")'
        lines.append(line)
    return "\n".join(lines)


def segment_messages(
    messages: list[Message],
    now: datetime,
    lull: timedelta,
    max_messages: int,
    overlap: int,
) -> list[Segment]:
    """Split new messages (ascending tg_message_id) into CLOSED segments.

    A segment is closed when followed by a gap > lull, when it reaches
    max_messages, or when the chat has been silent past the lull. Trailing
    messages in a still-active conversation stay unchunked.
    """
    by_thread: dict[int | None, list[Message]] = {}
    for m in messages:
        by_thread.setdefault(m.thread_id, []).append(m)

    segments: list[Segment] = []
    for thread_id, msgs in by_thread.items():
        current: list[Message] = []
        prev_tail: list[Message] = []
        for i, m in enumerate(msgs):
            current.append(m)
            is_last = i == len(msgs) - 1
            next_gap = None if is_last else msgs[i + 1].sent_at - m.sent_at
            closed = (
                len(current) >= max_messages
                or (next_gap is not None and next_gap > lull)
                or (is_last and (now - m.sent_at) > lull)
            )
            if closed:
                segments.append(
                    Segment(
                        thread_id=thread_id,
                        messages=prev_tail + current,
                        tg_id_start=current[0].tg_message_id,
                        tg_id_end=current[-1].tg_message_id,
                    )
                )
                prev_tail = current[-overlap:] if overlap else []
                current = []
    return segments


async def chunk_chat(
    session: AsyncSession,
    chat_id: int,
    now: datetime,
    lull: timedelta,
    max_messages: int,
    overlap: int,
) -> int:
    """Advance the chat's chunk cursor over newly closed segments."""
    state = await session.get(ChunkState, chat_id)
    if state is None:
        state = ChunkState(chat_id=chat_id, last_tg_message_id=0)
        session.add(state)

    messages = (
        (
            await session.execute(
                select(Message)
                .where(Message.chat_id == chat_id, Message.tg_message_id > state.last_tg_message_id)
                .order_by(Message.tg_message_id)
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    if not messages:
        return 0

    normalized_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    for m in messages:
        if m.sent_at.tzinfo is None:  # sqlite in tests
            m.sent_at = m.sent_at.replace(tzinfo=timezone.utc)

    segments = segment_messages(list(messages), normalized_now, lull, max_messages, overlap)

    # The cursor is shared across threads, so it may only advance past
    # messages every thread has closed — otherwise an active thread's
    # unchunked tail would be skipped forever. Segments beyond the safe
    # point are recomputed (identically) on a later pass.
    closed_end: dict[int | None, int] = {}
    for seg in segments:
        closed_end[seg.thread_id] = max(closed_end.get(seg.thread_id, 0), seg.tg_id_end)
    unclosed = [
        m.tg_message_id
        for m in messages
        if m.tg_message_id > closed_end.get(m.thread_id, 0)
    ]
    new_cursor = min(unclosed) - 1 if unclosed else messages[-1].tg_message_id
    if new_cursor <= state.last_tg_message_id:
        return 0

    # Resolve reply targets that live outside the fetched batch (one query);
    # in-batch targets are already at hand.
    by_id = {m.tg_message_id: m for m in messages}
    missing = {
        m.reply_to_tg_message_id
        for m in messages
        if m.reply_to_tg_message_id and m.reply_to_tg_message_id not in by_id
    }
    if missing:
        fetched = (
            await session.execute(
                select(Message).where(
                    Message.chat_id == chat_id, Message.tg_message_id.in_(missing)
                )
            )
        ).scalars()
        by_id.update({m.tg_message_id: m for m in fetched})

    persisted = 0
    for seg in segments:
        if seg.tg_id_end > new_cursor:
            continue
        session.add(
            Chunk(
                chat_id=chat_id,
                thread_id=seg.thread_id,
                msg_tg_id_start=seg.tg_id_start,
                msg_tg_id_end=seg.tg_id_end,
                text=render_segment(seg, by_id),
            )
        )
        persisted += 1
    state.last_tg_message_id = new_cursor
    return persisted
