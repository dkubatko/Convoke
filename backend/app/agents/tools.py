"""Memory tools attached to every agent run."""

from datetime import datetime, timezone

from pydantic_ai import RunContext
from sqlalchemy import select

from app.agents.deps import AgentDeps
from app.members import refresh_chat_memory_names, set_override_name
from app.memory.chunker import render_for_chat
from app.memory.store import search_chat_history as store_search
from app.models import ChatMember, IntentEpisode, Message, Note

MAX_NOTES_RETURNED = 8
MAX_PAST_ACTIONS = 6
MAX_MESSAGES_FETCHED = 30


async def search_chat_history(ctx: RunContext[AgentDeps], query: str) -> str:
    """Semantically search this chat's full message history (including
    imported history from before you joined). Use for anything that happened
    earlier than the recent messages you were shown."""
    async with ctx.deps.sessionmaker() as session:
        hits = await store_search(session, ctx.deps.embedder, ctx.deps.chat_id, query, k=4)
    if not hits:
        return "No matching history found."
    return "\n\n---\n\n".join(h.rendered for h in hits)


async def get_messages(ctx: RunContext[AgentDeps], message_ids: list[int]) -> str:
    """Fetch specific messages from this chat by id — the #id labels shown in
    transcripts and in "(replying to #id)" annotations. Use it to read a reply
    target or any cited message verbatim (up to 30 ids per call)."""
    ids = list(dict.fromkeys(message_ids))[:MAX_MESSAGES_FETCHED]
    if not ids:
        return "No message ids given."
    async with ctx.deps.sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.chat_id == ctx.deps.chat_id,
                        Message.tg_message_id.in_(ids),
                    )
                    .order_by(Message.tg_message_id)
                )
            )
            .scalars()
            .all()
        )
        out: list[str] = []
        if rows:
            out.append(await render_for_chat(session, ctx.deps.chat_id, list(rows)))
        found = {m.tg_message_id for m in rows}
        for mid in ids:
            if mid not in found:
                out.append(f"#{mid}: not in Convoke's stored history for this chat.")
    return "\n".join(out)


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
        # During an embedding-model swap the vector column may not match this
        # embedder yet — save unembedded; the re-embed job backfills.
        from app.models import EmbeddingState

        state = await session.get(EmbeddingState, 1)
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
        from app.models import EmbeddingState

        state = await session.get(EmbeddingState, 1)
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
        fired = e.fired_at.strftime("%Y-%m-%d %H:%M UTC")
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
    remember,
    recall,
    past_workflow_actions,
    list_members,
    set_member_name,
]
