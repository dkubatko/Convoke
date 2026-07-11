"""Memory tools attached to every agent run."""

from datetime import datetime, timedelta, timezone

from pydantic_ai import RunContext
from sqlalchemy import func, select, true

from app.agents.deps import AgentDeps
from app.core.config import get_tzinfo
from app.members import refresh_chat_memory_names, set_override_name
from app.memory.chunker import render_for_chat, render_ts
from app.memory.store import search_chat_history as store_search
from app.models import ChatMember, IntentEpisode, Message, Note
from app.threads import unmonitored_threads

MAX_NOTES_RETURNED = 8
MAX_PAST_ACTIONS = 6
MAX_MESSAGES_FETCHED = 30
# A context window is same-thread by construction; the cap keeps one call's
# render bounded (~41 messages) while still covering a whole conversation burst.
MAX_CONTEXT_RADIUS = 20


def _parse_day(value: str | None, end_of_day: bool) -> datetime | None:
    """ISO date/datetime → aware datetime; a bare date means a calendar day in
    the reference timezone (CONVOKE_TIMEZONE_OVERRIDE, default UTC) and snaps
    to its start (after) or end (before), so after=X, before=X covers all of
    day X as the chat's members experience it. Explicit offsets win. Raises
    ValueError with a model-actionable message."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"'{value}' is not an ISO date (use YYYY-MM-DD).") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_tzinfo())
    if len(value.strip()) <= 10 and end_of_day:  # bare date
        parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


async def search_chat_history(
    ctx: RunContext[AgentDeps],
    query: str,
    after: str | None = None,
    before: str | None = None,
) -> str:
    """Semantically search this chat's full message history (including
    imported history from before you joined). Use for anything that happened
    earlier than the recent messages you were shown. Optional after/before
    (ISO dates, e.g. '2026-05-05') restrict results to conversations from
    that period — use them for time-anchored questions ('last summer',
    'around her birthday'). To browse by date without a query, use
    get_messages_by_date."""
    try:
        after_dt = _parse_day(after, end_of_day=False)
        before_dt = _parse_day(before, end_of_day=True)
    except ValueError as e:
        return str(e)
    async with ctx.deps.sessionmaker() as session:
        # k=6, not 4: conversations ABOUT a fact (incl. with the bot) often
        # outscore the fact itself — measured live, the true answer sits at
        # fused rank 5-6 under that pressure. The model is good at picking
        # the right excerpt from a slightly bigger pile; two more are cheap.
        hits = await store_search(
            session, ctx.deps.embedder, ctx.deps.chat_id, query,
            k=6, after=after_dt, before=before_dt,
        )
    if not hits:
        return "No matching history found." + (
            " Try widening or dropping the date range." if after or before else ""
        )
    return "\n\n---\n\n".join(h.rendered for h in hits)


async def get_messages_by_date(
    ctx: RunContext[AgentDeps], date: str, radius: int = 10
) -> str:
    """Read the chat as it was on a given ISO date ('2025-10-19'): up to
    `radius` stored messages each side of that day's first message, as one
    transcript (max 20). Deterministic — no search ranking involved. Use it
    for 'what happened on/around <date>' and anniversary-style lookups; pair
    with search_chat_history when you don't know the date."""
    try:
        anchor_dt = _parse_day(date, end_of_day=False)
    except ValueError as e:
        return str(e)
    radius = max(1, min(radius, MAX_CONTEXT_RADIUS))
    async with ctx.deps.sessionmaker() as session:
        unmonitored = await unmonitored_threads(session, ctx.deps.chat_id)
        monitored = (
            ~func.coalesce(Message.thread_id, 0).in_(unmonitored) if unmonitored else true()
        )
        anchor = (
            await session.execute(
                select(Message)
                .where(
                    Message.chat_id == ctx.deps.chat_id,
                    Message.sent_at >= anchor_dt,
                    monitored,
                )
                .order_by(Message.tg_message_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        note = ""
        if anchor is None:  # date past the last stored message
            anchor = (
                await session.execute(
                    select(Message)
                    .where(Message.chat_id == ctx.deps.chat_id, monitored)
                    .order_by(Message.tg_message_id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if anchor is None:
                return "No stored messages in this chat."
            note = f"(No messages on/after {date} — showing the latest stored ones.)\n"
        before_rows = (
            await session.execute(
                select(Message)
                .where(
                    Message.chat_id == ctx.deps.chat_id,
                    Message.tg_message_id < anchor.tg_message_id,
                    monitored,
                )
                .order_by(Message.tg_message_id.desc())
                .limit(radius)
            )
        ).scalars().all()
        after_rows = (
            await session.execute(
                select(Message)
                .where(
                    Message.chat_id == ctx.deps.chat_id,
                    Message.tg_message_id > anchor.tg_message_id,
                    monitored,
                )
                .order_by(Message.tg_message_id)
                .limit(radius)
            )
        ).scalars().all()
        rows = list(reversed(before_rows)) + [anchor] + list(after_rows)
        return note + await render_for_chat(session, ctx.deps.chat_id, rows)


async def inspect_media(ctx: RunContext[AgentDeps], message_id: int, question: str) -> str:
    """Re-examine the media on one message with a specific question — the
    stored description is a short index entry and often lacks the detail
    asked about. Photos/videos are re-read by the vision model with your
    question (videos as sampled frames + audio transcript); voice returns
    its full transcript. Costs a model call — use when the stored
    description in the transcript doesn't answer. Unavailable for media from
    imported history (source files were discarded after description)."""
    from app.media.inspect import inspect_attachment

    async with ctx.deps.sessionmaker() as session:
        anchor = (
            await session.execute(
                select(Message).where(
                    Message.chat_id == ctx.deps.chat_id,
                    Message.tg_message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if anchor is None or (anchor.thread_id or 0) in await unmonitored_threads(
            session, ctx.deps.chat_id
        ):
            return f"#{message_id}: not in Convoke's stored history for this chat."
        return await inspect_attachment(session, ctx.deps.chat_id, message_id, question)


async def get_messages(ctx: RunContext[AgentDeps], message_ids: list[int]) -> str:
    """Fetch specific messages from this chat by id — the #id labels shown in
    transcripts and in "(replying to #id)" annotations. Use it to read a reply
    target or any cited message verbatim (up to 30 ids per call). To read the
    conversation AROUND one message, use get_conversation_context instead."""
    ids = list(dict.fromkeys(message_ids))[:MAX_MESSAGES_FETCHED]
    if not ids:
        return "No message ids given."
    async with ctx.deps.sessionmaker() as session:
        unmonitored = await unmonitored_threads(session, ctx.deps.chat_id)
        rows = [
            m
            for m in (
                await session.execute(
                    select(Message)
                    .where(
                        Message.chat_id == ctx.deps.chat_id,
                        Message.tg_message_id.in_(ids),
                    )
                    .order_by(Message.tg_message_id)
                )
            ).scalars()
            # Unmonitored threads are excluded from memory everywhere else;
            # an id-guess must not read them either. Rendered identically to
            # a missing id so their existence isn't leaked.
            if (m.thread_id or 0) not in unmonitored
        ]
        out: list[str] = []
        if rows:
            out.append(await render_for_chat(session, ctx.deps.chat_id, rows))
        found = {m.tg_message_id for m in rows}
        for mid in ids:
            if mid not in found:
                out.append(f"#{mid}: not in Convoke's stored history for this chat.")
    return "\n".join(out)


async def get_conversation_context(
    ctx: RunContext[AgentDeps], message_id: int, radius: int = 10
) -> str:
    """Read the conversation around one message: up to `radius` stored
    messages before and after it (same thread), rendered as one transcript.
    Use it when a search hit or fetched message ends mid-conversation and you
    need what was said around it — e.g. who a greeting was addressed to, or
    what decision followed a question. radius defaults to 10, max 20."""
    radius = max(1, min(radius, MAX_CONTEXT_RADIUS))
    async with ctx.deps.sessionmaker() as session:
        anchor = (
            await session.execute(
                select(Message).where(
                    Message.chat_id == ctx.deps.chat_id,
                    Message.tg_message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        not_stored = f"#{message_id}: not in Convoke's stored history for this chat."
        if anchor is None:
            return not_stored
        thread = anchor.thread_id or 0
        if thread in await unmonitored_threads(session, ctx.deps.chat_id):
            return not_stored  # unmonitored threads stay outside memory
        same_thread = (
            Message.chat_id == ctx.deps.chat_id,
            func.coalesce(Message.thread_id, 0) == thread,
        )
        before = (
            (
                await session.execute(
                    select(Message)
                    .where(*same_thread, Message.tg_message_id < message_id)
                    .order_by(Message.tg_message_id.desc())
                    .limit(radius)
                )
            )
            .scalars()
            .all()
        )
        after = (
            (
                await session.execute(
                    select(Message)
                    .where(*same_thread, Message.tg_message_id > message_id)
                    .order_by(Message.tg_message_id)
                    .limit(radius)
                )
            )
            .scalars()
            .all()
        )
        rows = list(reversed(before)) + [anchor] + list(after)
        return await render_for_chat(session, ctx.deps.chat_id, rows)


async def remember(ctx: RunContext[AgentDeps], fact: str, key: str | None = None) -> str:
    """Store a durable fact about this chat or its members (preferences,
    decisions, recurring context). Pass a short snake_case key to overwrite an
    existing note on the same topic instead of accumulating duplicates."""
    async with ctx.deps.sessionmaker() as session:
        note = None
        if key:
            note = (
                await session.execute(
                    select(Note).where(
                        Note.chat_id == ctx.deps.chat_id,
                        Note.key == key,
                        Note.deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
        # During a memory-model swap the vector column may not match this
        # embedder yet — save unembedded; the re-embed job backfills.
        from app.memory.runtime import embedding_state_for

        state = await embedding_state_for(session, "memory")
        reembedding = state is not None and state.status == "reembedding"
        embedding = None if reembedding else (await ctx.deps.embedder.embed_passages([fact]))[0]
        if note is None:
            note = Note(chat_id=ctx.deps.chat_id, key=key)
            session.add(note)
        note.text = fact
        note.embedding = embedding
        note.created_by_run_id = ctx.deps.run_id
        note.updated_at = datetime.now(timezone.utc)
        await session.commit()
    return f"Remembered{f' under key {key!r}' if key else ''}."


async def recall(ctx: RunContext[AgentDeps], query: str) -> str:
    """Search previously remembered notes for this chat."""
    async with ctx.deps.sessionmaker() as session:
        from app.memory.runtime import embedding_state_for

        state = await embedding_state_for(session, "memory")
        base = select(Note).where(Note.chat_id == ctx.deps.chat_id, Note.deleted.is_(False))
        if session.bind.dialect.name == "postgresql" and not (
            state is not None and state.status == "reembedding"
        ):
            qvec = await ctx.deps.embedder.embed_query(query)
            notes = (
                (
                    await session.execute(
                        base.where(Note.embedding.is_not(None))
                        .order_by(Note.embedding.cosine_distance(qvec))
                        .limit(MAX_NOTES_RETURNED)
                    )
                )
                .scalars()
                .all()
            )
        else:  # test fallback
            notes = (
                (await session.execute(base.order_by(Note.updated_at.desc()).limit(MAX_NOTES_RETURNED)))
                .scalars()
                .all()
            )
    if not notes:
        return "No notes stored for this chat yet."
    return "\n".join(f"- {(n.key + ': ') if n.key else ''}{n.text}" for n in notes)


async def past_workflow_actions(ctx: RunContext[AgentDeps]) -> str:
    """What the workflow that triggered you already did in this chat recently
    — each past action's topic, gathered details, outcome, and time. Check
    this before acting when the current task could be a follow-up to (or
    already covered by) an earlier action, and prefer updating or adjusting
    the earlier result over duplicating it."""
    if ctx.deps.workflow_id is None:
        return "This run was not triggered by a workflow — nothing to compare against."
    async with ctx.deps.sessionmaker() as session:
        episodes = (
            (
                await session.execute(
                    select(IntentEpisode)
                    .where(
                        IntentEpisode.workflow_id == ctx.deps.workflow_id,
                        IntentEpisode.chat_id == ctx.deps.chat_id,
                        IntentEpisode.fired_at.is_not(None),
                    )
                    .order_by(IntentEpisode.fired_at.desc())
                    .limit(MAX_PAST_ACTIONS + 1)  # +1: the current run's own episode
                )
            )
            .scalars()
            .all()
        )
    lines: list[str] = []
    for e in episodes:
        if e.agent_run_id == ctx.deps.run_id:
            continue  # the action currently being executed
        fired = render_ts(e.fired_at)
        lines.append(f"- [{fired}] {e.summary or '(no topic summary)'}")
        if e.slots:
            lines.append(
                "  details: "
                + "; ".join(f"{k}: {v.get('value')}" for k, v in sorted(e.slots.items()))
            )
        lines.append(f"  outcome: {e.execution_summary or 'in progress'}")
    if not lines:
        return "This workflow has taken no previous actions in this chat."
    return "\n".join(lines)


async def list_members(ctx: RunContext[AgentDeps]) -> str:
    """List the people in this chat and how you currently refer to each: their
    display name, @handle (if they have one), and stable user_id. Use it to
    answer "who's in here", to tell apart people with similar names, or to find
    whose user_id to pass to set_member_name. (Who is addressing you right now,
    with their user_id, is stated in your instructions.)"""
    async with ctx.deps.sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(ChatMember).where(ChatMember.chat_id == ctx.deps.chat_id)
                )
            )
            .scalars()
            .all()
        )
    if not rows:
        return "No known members yet."
    lines = []
    for m in sorted(rows, key=lambda r: r.display_name.lower()):  # sort by shown name
        handle = f" @{m.handle}" if m.handle else ""
        lines.append(f"- {m.display_name}{handle} — user_id {m.sender_id}")
    return "\n".join(lines)


async def set_member_name(ctx: RunContext[AgentDeps], user_id: int, name: str) -> str:
    """Change the display name for a chat member — the name you will use for
    them EVERYWHERE: in the conversation you are reading, in searched history,
    and in the roster list_members returns. Use it whenever someone asks to be
    called a certain name ("call me Даня", "my name is X"). For any OTHER fact
    about a person, use remember instead, not this. Pass the person's user_id —
    from list_members, or (for the person addressing you) from your
    instructions, which name them and give their user_id."""
    name = name.strip()
    if not name:
        return "Give a non-empty name."
    async with ctx.deps.sessionmaker() as session:
        member, changed = await set_override_name(session, ctx.deps.chat_id, user_id, name)
        if member is None:
            return (
                f"No member with user_id {user_id} in this chat. "
                "Call list_members first to find the right id."
            )
        display = member.display_name
        if changed:
            # History is rendered under the old name; refresh it in place (the
            # memory loop re-renders stale chunks — no memory outage).
            await refresh_chat_memory_names(session, ctx.deps.chat_id)
        await session.commit()
    return f"Done — I'll refer to user_id {user_id} as {display!r} from now on."


AGENT_TOOLS = [
    search_chat_history,
    get_messages,
    get_conversation_context,
    get_messages_by_date,
    inspect_media,
    remember,
    recall,
    past_workflow_actions,
    list_members,
    set_member_name,
]
