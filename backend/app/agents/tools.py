"""Memory tools attached to every agent run."""

from datetime import datetime, timezone

from pydantic_ai import RunContext
from sqlalchemy import select

from app.agents.deps import AgentDeps
from app.memory.store import search_chat_history as store_search
from app.models import IntentEpisode, Note

MAX_NOTES_RETURNED = 8
MAX_PAST_ACTIONS = 6


async def search_chat_history(ctx: RunContext[AgentDeps], query: str) -> str:
    """Semantically search this chat's full message history (including
    imported history from before you joined). Use for anything that happened
    earlier than the recent messages you were shown."""
    async with ctx.deps.sessionmaker() as session:
        hits = await store_search(session, ctx.deps.embedder, ctx.deps.chat_id, query, k=4)
    if not hits:
        return "No matching history found."
    return "\n\n---\n\n".join(h.rendered for h in hits)


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
        embedding = (await ctx.deps.embedder.embed_passages([fact]))[0]
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
        base = select(Note).where(Note.chat_id == ctx.deps.chat_id, Note.deleted.is_(False))
        if session.bind.dialect.name == "postgresql":
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


AGENT_TOOLS = [search_chat_history, remember, recall, past_workflow_actions]
