"""Context assembly under an explicit character budget.

Budget split ≈ 55% recent verbatim / 30% semantic hits / 15% notes, with the
triggering message always present (it is the tail of the recent window).
Semantic hits that overlap the recent window are dropped rather than repeated.
Rolling summaries land with the cheap-model infrastructure (M6).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.memory.chunker import render_message
from app.memory.embeddings import Embedder
from app.memory.store import search_chat_history
from app.models import Chat, MemoryGap, Message, Note

RECENT_SHARE = 0.55
HITS_SHARE = 0.30
NOTES_SHARE = 0.15
MAX_CONTEXT_NOTES = 10


async def assemble_context(
    session: AsyncSession,
    embedder: Embedder,
    chat: Chat,
    query_text: str,
    thread_id: int | None = None,
) -> str:
    budget = get_settings().context_char_budget

    # Recent messages, newest first until the share is spent, then re-reversed.
    stmt = select(Message).where(Message.chat_id == chat.id)
    if thread_id is not None:
        stmt = stmt.where(Message.thread_id == thread_id)
    recent_rows = (
        (await session.execute(stmt.order_by(Message.tg_message_id.desc()).limit(80)))
        .scalars()
        .all()
    )
    recent: list[Message] = []
    used = 0
    for m in recent_rows:
        line = render_message(m)
        if used + len(line) > budget * RECENT_SHARE and recent:
            break
        recent.append(m)
        used += len(line)
    recent.reverse()

    hits_text: list[str] = []
    if query_text.strip():
        hits = await search_chat_history(session, embedder, chat.id, query_text, k=5)
        # dedup: drop hits already shown verbatim in the recent window
        if recent:
            first_recent = render_message(recent[0])
            hits = [h for h in hits if first_recent not in h.rendered]
        hits_used = 0
        for h in hits:
            if not h.rendered or hits_used + len(h.rendered) > budget * HITS_SHARE:
                continue  # try a smaller later hit rather than stopping cold
            hits_text.append(h.rendered)
            hits_used += len(h.rendered)

    notes = (
        (
            await session.execute(
                select(Note)
                .where(Note.chat_id == chat.id, Note.deleted.is_(False))
                .order_by(Note.updated_at.desc())
                .limit(MAX_CONTEXT_NOTES)
            )
        )
        .scalars()
        .all()
    )
    notes_used = 0
    notes_lines: list[str] = []
    for n in notes:
        line = f"- {(n.key + ': ') if n.key else ''}{n.text}"
        if notes_used + len(line) > budget * NOTES_SHARE:
            break
        notes_lines.append(line)
        notes_used += len(line)

    gaps = (
        (
            await session.execute(
                select(MemoryGap)
                .where(MemoryGap.chat_id == chat.id)
                .order_by(MemoryGap.gap_end.desc())
                .limit(3)
            )
        )
        .scalars()
        .all()
    )

    sections: list[str] = []
    if gaps:
        sections.append(
            "## Known gaps in memory (the bot was offline; messages in these "
            "ranges were never seen)\n"
            + "\n".join(
                f"- {g.gap_start:%Y-%m-%d %H:%M} to {g.gap_end:%Y-%m-%d %H:%M} UTC"
                for g in gaps
            )
        )
    if notes_lines:
        sections.append("## Remembered notes about this chat\n" + "\n".join(notes_lines))
    if hits_text:
        sections.append(
            "## Possibly relevant older conversations\n" + "\n\n---\n\n".join(hits_text)
        )
    if recent:
        sections.append(
            "## Recent messages (most recent last)\n"
            + "\n".join(render_message(m) for m in recent)
        )
    return "\n\n".join(sections)
