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
from app.members import load_member_names
from app.models import Chunk, ChunkState, Message
from app.threads import unmonitored_threads


@dataclass
class Segment:
    thread_id: int | None
    messages: list[Message]  # includes overlap prefix
    tg_id_start: int  # excludes overlap: cursor range actually covered
    tg_id_end: int


def _member_name(m: Message, names: dict[int, str]) -> str:
    """The name to show for a message's sender: the chat-member mapping
    (`sender_id -> name`) when present, else the raw per-message name — so one
    person reads consistently across imported history and live traffic. `names`
    is required (pass {} when there are none) so a caller can't silently leak
    raw names by forgetting to resolve them."""
    mapped = names.get(m.sender_id) if names and m.sender_id is not None else None
    return mapped or m.sender_name or "Unknown"


def render_message(m: Message, names: dict[int, str]) -> str:
    """One transcript line: 'Sender [ts] #id: body'. The #id is the real
    Telegram message id — every reader (agent context, chunks, search hits,
    the intent classifier) uses it identically: agents pass it to
    get_messages, and reply annotations point at it. `names` resolves the
    sender via the chat-member map (see `_member_name`)."""
    ts = m.sent_at.strftime("%Y-%m-%d %H:%M")
    return f"{_member_name(m, names)} [{ts}] #{m.tg_message_id}: {message_body(m)}"


def reply_quote(target: Message, names: dict[int, str], limit: int = 120) -> str:
    """The quoted-original line for a reply whose target isn't visible in the
    same transcript — rendered in the same 'Sender [ts] #id: body' shape as a
    normal line so it reads uniformly. Single source of the ↳ format."""
    ts = target.sent_at.strftime("%Y-%m-%d %H:%M")
    q = message_body(target).replace("\n", " ")
    if len(q) > limit:
        q = q[:limit] + "…"
    return (
        f'  ↳ replies to [#{target.tg_message_id}] [{ts}] '
        f'{_member_name(target, names)}: "{q}"'
    )


def reply_annotation(
    m: Message,
    present: set[int],
    targets: dict[int, Message],
    names: dict[int, str],
) -> str:
    """The reply-linkage suffix for one transcript line — the single source of
    reply rendering, shared by every transcript (agent context, chunks, search
    hits, the intent classifier): a pure pointer when the target is visible in
    the same transcript, the quoted original when it is off-screen, the bare
    id when Convoke never stored it. Empty when the message is not a reply."""
    rid = m.reply_to_tg_message_id
    if not rid:
        return ""
    if rid in present:
        return f" (replying to #{rid})"
    target = targets.get(rid)
    if target is not None and message_body(target):
        return "\n" + reply_quote(target, names)
    return f" (replying to #{rid} — message not stored)"


def render_thread(
    messages: list[Message],
    reply_targets: dict[int, Message],
    names: dict[int, str],
) -> str:
    """Render messages as a transcript with reply linkage always explicit.
    Chunks embed this text, so both their embeddings and search_chat_history
    hits carry the linkage. `names` resolves senders via the chat-member map.

    Prefer `render_for_chat` unless you already hold the names map + reply
    targets (e.g. rendering many segments of one chat) — it loads both for you
    so no caller has to remember to thread `names` through by hand."""
    present = {m.tg_message_id for m in messages}
    targets = reply_targets or {}
    return "\n".join(
        render_message(m, names) + reply_annotation(m, present, targets, names)
        for m in messages
    )


async def render_for_chat(
    session: AsyncSession,
    chat_id: int,
    messages: list[Message],
    names: dict[int, str] | None = None,
) -> str:
    """The one entry point for turning a chat's message rows into a model-facing
    transcript: loads the member-name map (unless the caller already has it) and
    resolves reply targets, then renders. Because callers never hand-thread
    `names`, the class of "forgot to resolve names → raw names leak into memory"
    bug can't recur here. `names` is an optional perf hint for batch callers that
    render many chunks of one chat and load the map once."""
    if names is None:
        names = await load_member_names(session, chat_id)
    targets = await resolve_reply_targets(session, chat_id, list(messages))
    return render_thread(messages, targets, names)


async def resolve_reply_targets(
    session: AsyncSession, chat_id: int, messages: list[Message]
) -> dict[int, Message]:
    """Reply targets for `messages`, keyed by tg_message_id: in-batch targets
    come for free, the rest are fetched in one query. Shared by chunking,
    agent context, tools, and the API."""
    by_id = {m.tg_message_id: m for m in messages}
    targets: dict[int, Message] = {}
    missing: set[int] = set()
    for m in messages:
        rid = m.reply_to_tg_message_id
        if not rid:
            continue
        if rid in by_id:
            targets[rid] = by_id[rid]
        else:
            missing.add(rid)
    if missing:
        fetched = (
            await session.execute(
                select(Message).where(
                    Message.chat_id == chat_id, Message.tg_message_id.in_(missing)
                )
            )
        ).scalars()
        targets.update({m.tg_message_id: m for m in fetched})
    return targets


def segment_messages(
    messages: list[Message],
    now: datetime,
    lull: timedelta,
    max_messages: int,
    overlap: int,
    truncated: bool = False,
) -> list[Segment]:
    """Split new messages (ascending tg_message_id) into CLOSED segments.

    A segment is closed when followed by a gap > lull, when it reaches
    max_messages, or when the chat has been silent past the lull. Trailing
    messages in a still-active conversation stay unchunked. `truncated` means
    the batch was cut short of the chat's real tail — the silence rule can't
    tell a lull from the batch edge then, so it is skipped and the tail waits
    for a later pass.
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
                or (is_last and not truncated and (now - m.sent_at) > lull)
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
    limit: int = 2000,
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
                .limit(limit)
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

    # Unmonitored threads are never chunked into memory — but the cursor must
    # still advance past them, so they are dropped from segmentation and from
    # the unclosed set below rather than left to block it forever.
    unmonitored = await unmonitored_threads(session, chat_id)
    chunkable = [m for m in messages if (m.thread_id or 0) not in unmonitored]

    # A full batch means more messages exist beyond it — the batch-final
    # message is not chat-final, so the silence rule must not close its
    # segment at an arbitrary boundary (import re-chunk would cut every
    # `limit` messages otherwise).
    truncated = len(messages) == limit
    segments = segment_messages(
        chunkable, normalized_now, lull, max_messages, overlap, truncated
    )

    # The cursor is shared across threads, so it may only advance past
    # messages every monitored thread has closed — otherwise an active thread's
    # unchunked tail would be skipped forever. Threads interleave in tg-id
    # space, so a closed segment held back this way may still START at or
    # below that point; clamp under any such segment (to a fixpoint) so it is
    # recomputed in full on a later pass instead of losing its early messages
    # behind the cursor.
    closed_end: dict[int | None, int] = {}
    for seg in segments:
        closed_end[seg.thread_id] = max(closed_end.get(seg.thread_id, 0), seg.tg_id_end)
    unclosed = [
        m.tg_message_id
        for m in chunkable
        if m.tg_message_id > closed_end.get(m.thread_id, 0)
    ]
    new_cursor = min(unclosed) - 1 if unclosed else messages[-1].tg_message_id
    while skipped := [
        s.tg_id_start
        for s in segments
        if s.tg_id_end > new_cursor and s.tg_id_start <= new_cursor
    ]:
        new_cursor = min(skipped) - 1
    if new_cursor <= state.last_tg_message_id:
        return 0

    targets = await resolve_reply_targets(session, chat_id, list(messages))
    names = await load_member_names(session, chat_id)

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
                text=render_thread(seg.messages, targets, names),
            )
        )
        persisted += 1
    state.last_tg_message_id = new_cursor
    return persisted
