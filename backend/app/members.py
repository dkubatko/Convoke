"""Chat-member identity: the single place that resolves a stable Telegram user
id to the name we show, and that keeps the `chat_members` table current.

Rendering resolves `sender_id -> override_name ?? auto_name` via
`load_member_names`; ingest keeps `auto_name`/`handle` fresh via `upsert_member`
(latest-wins by sent_at, so importing OLD history never clobbers a newer
live-derived name). `sender_name` on the message row is untouched — it stays the
raw input this table derives from, and the fallback when a sender has no row.
"""

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.intent.episodes import as_utc  # naive(sqlite)->UTC coercion for comparisons
from app.models import ChatMember, Chunk, ChunkState

MAX_NAME_LEN = 64


def clean_display_name(name: str | None, max_len: int = MAX_NAME_LEN) -> str:
    """Normalize a name before it can reach model-visible text: collapse every
    whitespace run to one space — an embedded newline would let a name forge
    extra lines in the line-oriented transcript render — and cap the length so
    a pathological name can't bloat every chunk and prompt it appears in."""
    return " ".join((name or "").split())[:max_len]


async def load_member_names(session: AsyncSession, chat_id: int) -> dict[int, str]:
    """`sender_id -> display name` for a chat (override wins over auto). Empty
    names are dropped so the renderer falls back to the raw message name."""
    rows = (
        await session.execute(
            select(
                ChatMember.sender_id, ChatMember.auto_name, ChatMember.override_name
            ).where(ChatMember.chat_id == chat_id)
        )
    ).all()
    out: dict[int, str] = {}
    for sender_id, auto_name, override_name in rows:
        name = override_name or auto_name
        if name:
            out[sender_id] = name
    return out


async def member_display_name(
    session: AsyncSession, chat_id: int, sender_id: int
) -> str | None:
    """The resolved name for a single sender (override ?? auto), or None when
    there's no row or the name is empty. Used to name the requester in an agent
    run's instructions."""
    member = await session.get(ChatMember, (chat_id, sender_id))
    if member is None:
        return None
    return member.override_name or member.auto_name or None


async def upsert_member(
    session: AsyncSession,
    chat_id: int,
    sender_id: int | None,
    name: str | None,
    sent_at: datetime | None,
    handle: str | None = None,
    update_handle: bool = False,
    require_newer: bool = False,
) -> None:
    """Record/refresh one member from an observed message. `auto_name` only
    advances when this message is at least as new as the one that last set it
    (latest-wins) so a historical import can't overwrite a live name. Never
    touches `override_name`.

    `require_newer=True` (imports) makes the comparison strictly newer: an
    export that contains the very message that set the live name carries the
    exporter's label for it (often a phone-contact name) at the SAME sent_at —
    a tie must keep the live-derived name.

    `update_handle=True` (live messages, which carry the authoritative
    username) writes `handle` verbatim, including None to clear a removed
    username; imports leave the handle untouched."""
    if sender_id is None:
        return
    clean = clean_display_name(name)
    member = await session.get(ChatMember, (chat_id, sender_id))
    if member is None:
        # INSERT .. ON CONFLICT DO NOTHING: a concurrent ingest path (live
        # handler vs. an import's roster refresh) may create the row between
        # our get and the insert; a plain add would abort the whole
        # transaction on the PK collision. Whoever loses falls through to the
        # ordinary latest-wins update below.
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        await session.execute(
            insert(ChatMember)
            .values(
                chat_id=chat_id,
                sender_id=sender_id,
                auto_name=clean,
                # Only anchor the basis when we have a real name, so a later
                # (even older-timestamped) message with a name can still set
                # auto_name instead of being locked out by an empty sighting.
                name_basis_at=sent_at if clean else None,
            )
            .on_conflict_do_nothing(index_elements=["chat_id", "sender_id"])
        )
        member = await session.get(ChatMember, (chat_id, sender_id))
        if member is None:  # unreachable: either insert or the rival's row
            return
    basis = as_utc(member.name_basis_at)
    this = as_utc(sent_at)
    fresher = basis is None or (
        this is not None and (this > basis if require_newer else this >= basis)
    )
    if clean and fresher:
        # Also runs on an equal name: the basis must track the LATEST sighting,
        # or an import timestamped between two live sightings could pass the
        # strictly-newer check and clobber the name.
        member.auto_name = clean
        member.name_basis_at = sent_at
    if update_handle and member.handle != handle:  # skip a no-op UPDATE on repeats
        member.handle = handle


async def refresh_members_from_messages(
    session: AsyncSession, chat_id: int, observed: dict[int, tuple[str, datetime]]
) -> None:
    """Bulk variant for batch ingest (imports): `observed` maps sender_id ->
    (latest name, its sent_at). Same latest-wins rule, but strictly-newer —
    an import must never clobber a live-derived name on a timestamp tie."""
    for sender_id, (name, sent_at) in observed.items():
        await upsert_member(session, chat_id, sender_id, name, sent_at, require_newer=True)


async def set_override_name(
    session: AsyncSession, chat_id: int, sender_id: int, name: str | None
) -> tuple[ChatMember | None, bool]:
    """Operator/agent override. Blank clears it back to the auto name. Returns
    (row, changed) — `changed` is False for a no-op so callers can skip the
    (expensive) re-chunk. `row` is None if the member doesn't exist."""
    member = await session.get(ChatMember, (chat_id, sender_id))
    if member is None:
        return None, False
    new = clean_display_name(name) or None
    if new == member.auto_name:
        # Typing the auto name back IS clearing the override — storing it would
        # count as a change and trigger a pointless full memory rebuild.
        new = None
    changed = new != member.override_name
    member.override_name = new
    return member, changed


async def invalidate_chat_memory(session: AsyncSession, chat_id: int) -> None:
    """Drop the chat's chunks + cursor so the memory loop re-chunks from
    scratch — for imports, where the MESSAGE SET itself changed. For a rename
    use refresh_chat_memory_names instead: segmentation doesn't depend on
    names, so a full rebuild would only leave the chat memoryless for the
    duration."""
    await session.execute(delete(Chunk).where(Chunk.chat_id == chat_id))
    await session.execute(delete(ChunkState).where(ChunkState.chat_id == chat_id))


async def refresh_chat_memory_names(session: AsyncSession, chat_id: int) -> None:
    """After a display-name change: mark every chunk stale so the memory loop
    re-renders it from raw rows (which resolves the new name) and re-embeds in
    place. Old text/vectors stay searchable until each chunk is refreshed — no
    memory outage, no cursor reset. The content_version bump guards against a
    rename racing an in-flight embed (same contract as message edits)."""
    await session.execute(
        update(Chunk)
        .where(Chunk.chat_id == chat_id)
        .values(stale=True, content_version=Chunk.content_version + 1)
    )
