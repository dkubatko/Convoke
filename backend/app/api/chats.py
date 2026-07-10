from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import ChatOut
from app.core.config import get_settings
from app.core.db import get_session, get_sessionmaker
from app.core.tasks import spawn
from app.core.security import require_operator
from app.ingest.history_import import delete_import, run_import
from app.media.render import message_body
from app.members import (
    load_member_names,
    refresh_chat_memory_names,
    set_bot_flag,
    set_override_name,
)
from app.memory.chunker import resolve_reply_targets
from app.memory.runtime import ensure_embedder
from app.memory.store import search_chat_history
from app.models import (
    AgentRun,
    Chat,
    ChatMember,
    ChatThread,
    ImportJob,
    MemoryGap,
    Message,
    MessageAttachment,
    Note,
)

router = APIRouter(dependencies=[Depends(require_operator)])


class ReplyPreview(BaseModel):
    sender_name: str
    text: str


class AttachmentOut(BaseModel):
    kind: str
    status: str
    description: str | None
    transcript: str | None
    error: str | None
    duration_s: int | None

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    tg_message_id: int
    sender_name: str
    text: str
    sent_at: datetime
    source: str
    # The quoted original when this message is a Telegram reply.
    reply_to: ReplyPreview | None = None
    attachment: AttachmentOut | None = None

    model_config = {"from_attributes": True}


class SearchHitOut(BaseModel):
    chunk_id: int
    score: float  # fused hybrid-retrieval score; ranks hits within one response
    rendered: str


class ImportJobOut(BaseModel):
    id: int
    chat_id: int
    filename: str
    status: str
    detail: str | None
    messages_total: int
    messages_ingested: int
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


async def _chat_or_404(session: AsyncSession, chat_id: int) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    return chat


@router.get("/chats/{chat_id}", response_model=ChatOut)
async def get_chat(chat_id: int, session: AsyncSession = Depends(get_session)) -> Chat:
    """Single chat — lets the detail header poll for status changes (e.g. the
    admin authorizing in Telegram) instead of fetching the whole list once."""
    return await _chat_or_404(session, chat_id)


# ---------- threads (per-thread monitoring) ----------


class ThreadPreviewMsg(BaseModel):
    sender_name: str
    text: str
    sent_at: datetime


class ThreadOut(BaseModel):
    thread_key: int  # 0 = General/main thread
    name: str  # effective display name: title if set, else default_name
    title: str | None  # operator/captured title (None = using the default)
    default_name: str  # "General" or "Topic #N"
    monitored: bool
    message_count: int
    last_activity: datetime | None
    preview: list[ThreadPreviewMsg]  # most recent messages, oldest-first


class ThreadUpdate(BaseModel):
    monitored: bool | None = None
    # "" or whitespace clears back to the default
    title: str | None = Field(default=None, max_length=128)


async def _list_threads(
    session: AsyncSession, chat_id: int, preview_n: int
) -> list[ThreadOut]:
    # Threads are discovered from stored messages; a captured-but-empty topic
    # (only its service message, which isn't stored) is folded in from its row.
    tk_col = func.coalesce(Message.thread_id, 0)
    rows = (
        await session.execute(
            select(
                tk_col.label("tk"),
                func.count().label("cnt"),
                func.min(Message.tg_message_id).label("first"),
                func.max(Message.sent_at).label("last"),
            )
            .where(Message.chat_id == chat_id)
            .group_by(tk_col)
        )
    ).all()
    meta = {
        r.thread_key: r
        for r in (
            await session.execute(select(ChatThread).where(ChatThread.chat_id == chat_id))
        ).scalars()
    }
    stats = {r.tk: r for r in rows}
    keys = set(stats) | set(meta)
    # Previews show the same resolved names as every other surface — raw
    # per-message sender_name would diverge from Members/Messages on a rename.
    names = await load_member_names(session, chat_id)
    # Ordinal names for non-General threads by order of first appearance
    # (message-less named topics sort last).
    non_general = sorted(
        (k for k in keys if k != 0),
        key=lambda k: stats[k].first if k in stats else float("inf"),
    )
    ordinal = {k: i + 1 for i, k in enumerate(non_general)}

    out: list[ThreadOut] = []
    for tk in [0, *non_general] if 0 in keys else non_general:
        st = stats.get(tk)
        m = meta.get(tk)
        default_name = "General" if tk == 0 else f"Topic #{ordinal[tk]}"
        title = m.title if m else None
        preview: list[ThreadPreviewMsg] = []
        if st is not None and preview_n > 0:
            pv = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.chat_id == chat_id,
                            Message.thread_id.is_(None) if tk == 0 else Message.thread_id == tk,
                        )
                        .order_by(Message.tg_message_id.desc())
                        .limit(preview_n)
                    )
                )
                .scalars()
                .all()
            )
            preview = [
                ThreadPreviewMsg(
                    sender_name=(names.get(p.sender_id) if p.sender_id is not None else None)
                    or p.sender_name,
                    text=(message_body(p) or "").replace("\n", " ")[:160],
                    sent_at=p.sent_at,
                )
                for p in reversed(pv)
            ]
        out.append(
            ThreadOut(
                thread_key=tk,
                name=title or default_name,
                title=title,
                default_name=default_name,
                monitored=m.monitored if m else True,
                message_count=st.cnt if st else 0,
                last_activity=st.last if st else None,
                preview=preview,
            )
        )
    return out


@router.get("/chats/{chat_id}/threads", response_model=list[ThreadOut])
async def list_threads(
    chat_id: int, preview: int = 5, session: AsyncSession = Depends(get_session)
) -> list[ThreadOut]:
    await _chat_or_404(session, chat_id)
    return await _list_threads(session, chat_id, max(0, min(preview, 20)))


@router.put("/chats/{chat_id}/threads/{thread_key}", response_model=list[ThreadOut])
async def update_thread(
    chat_id: int,
    thread_key: int,
    body: ThreadUpdate,
    session: AsyncSession = Depends(get_session),
) -> list[ThreadOut]:
    await _chat_or_404(session, chat_id)
    row = await session.get(ChatThread, (chat_id, thread_key))
    if row is None:
        # Only keys _list_threads would discover are real: 0 (General) always,
        # otherwise the thread must have messages. Anything else would mint a
        # phantom topic row for an arbitrary integer.
        if thread_key != 0:
            has_messages = (
                await session.execute(
                    select(Message.id)
                    .where(Message.chat_id == chat_id, Message.thread_id == thread_key)
                    .limit(1)
                )
            ).first()
            if has_messages is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "No such thread in this chat")
        row = ChatThread(chat_id=chat_id, thread_key=thread_key)
        session.add(row)
    if body.monitored is not None:
        row.monitored = body.monitored
    if body.title is not None:
        row.title = body.title.strip() or None  # blank clears back to the default
    await session.commit()
    return await _list_threads(session, chat_id, 5)


class MemberOut(BaseModel):
    sender_id: int
    handle: str | None
    auto_name: str  # latest observed name (raw)
    override_name: str | None  # operator/agent label
    display_name: str  # effective: override_name or auto_name
    is_bot: bool  # messages render [bot] and are excluded from memory scoring


class MemberUpdate(BaseModel):
    # blank/None clears the override back to auto_name
    display_name: str | None = Field(max_length=64)
    is_bot: bool | None = None  # None: leave unchanged


class MemberOverride(BaseModel):
    sender_id: int
    # blank/None clears the override back to auto_name
    display_name: str | None = Field(max_length=64)
    is_bot: bool | None = None  # None: leave unchanged


def _member_out(m: ChatMember) -> MemberOut:
    return MemberOut(
        sender_id=m.sender_id,
        handle=m.handle,
        auto_name=m.auto_name,
        override_name=m.override_name,
        display_name=m.display_name,
        is_bot=m.is_bot,
    )


async def _members_out(session: AsyncSession, chat_id: int) -> list[MemberOut]:
    rows = (
        (await session.execute(select(ChatMember).where(ChatMember.chat_id == chat_id)))
        .scalars()
        .all()
    )
    # Stable order — by the underlying Telegram name (which a UI rename doesn't
    # touch), then id — so renaming a member never reshuffles the table.
    return [_member_out(m) for m in sorted(rows, key=lambda r: (r.auto_name.lower(), r.sender_id))]


@router.get("/chats/{chat_id}/members", response_model=list[MemberOut])
async def list_chat_members(
    chat_id: int, session: AsyncSession = Depends(get_session)
) -> list[MemberOut]:
    await _chat_or_404(session, chat_id)
    return await _members_out(session, chat_id)


@router.put("/chats/{chat_id}/members", response_model=list[MemberOut])
async def update_chat_members(
    chat_id: int,
    body: list[MemberOverride],
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    """Batch-apply display-name overrides (the Members form saves all edits at
    once). Memory is refreshed once, only if something actually changed."""
    await _chat_or_404(session, chat_id)
    changed_any = False
    for o in body:
        member, changed = await set_override_name(session, chat_id, o.sender_id, o.display_name)
        if member is None:  # same contract as the single-member endpoint
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"No member {o.sender_id} in this chat"
            )
        if o.is_bot is not None:
            _, flag_changed = await set_bot_flag(session, chat_id, o.sender_id, o.is_bot)
            changed = changed or flag_changed
        changed_any = changed_any or changed
    if changed_any:
        # History embeds the old names — refresh in place, no memory outage.
        await refresh_chat_memory_names(session, chat_id)
    await session.commit()
    return await _members_out(session, chat_id)


@router.put("/chats/{chat_id}/members/{sender_id}", response_model=MemberOut)
async def update_chat_member(
    chat_id: int,
    sender_id: int,
    body: MemberUpdate,
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    await _chat_or_404(session, chat_id)
    member, changed = await set_override_name(session, chat_id, sender_id, body.display_name)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such member in this chat")
    if body.is_bot is not None:
        # The flag changes both the [bot] tag in renders and the embedding
        # input, so it shares the rename contract: real change -> refresh.
        _, flag_changed = await set_bot_flag(session, chat_id, sender_id, body.is_bot)
        changed = changed or flag_changed
    out = _member_out(member)
    if changed:
        # History is rendered/embedded under the old name — refresh it in
        # place (stale-mark; no memory outage). Skip for a no-op save.
        await refresh_chat_memory_names(session, chat_id)
    await session.commit()
    return out


@router.get("/chats/{chat_id}/messages", response_model=list[MessageOut])
async def recent_messages(
    chat_id: int, limit: int = 50, session: AsyncSession = Depends(get_session)
) -> list[MessageOut]:
    await _chat_or_404(session, chat_id)
    rows = (
        (
            await session.execute(
                select(Message)
                .where(Message.chat_id == chat_id)
                .order_by(Message.tg_message_id.desc())
                .limit(max(1, min(limit, 200)))
            )
        )
        .scalars()
        .all()
    )
    # Resolve replied-to previews (usually within `rows`; one extra query
    # covers replies to older messages).
    targets = await resolve_reply_targets(session, chat_id, list(rows))
    # Show the same (override/auto) names the Members tab and the bot use, so
    # the Messages list doesn't diverge from the rest of the app after a rename.
    names = await load_member_names(session, chat_id)

    def display(m: Message) -> str:
        return (names.get(m.sender_id) if m.sender_id is not None else None) or m.sender_name or "Unknown"

    def preview(m: Message) -> ReplyPreview | None:
        target = targets.get(m.reply_to_tg_message_id or 0)
        if target is None:
            return None
        text = message_body(target).replace("\n", " ")
        return ReplyPreview(
            sender_name=display(target),
            text=text[:140] + ("…" if len(text) > 140 else ""),
        )

    return [
        MessageOut(
            id=m.id, tg_message_id=m.tg_message_id, sender_name=display(m),
            text=m.text, sent_at=m.sent_at, source=m.source, reply_to=preview(m),
            attachment=AttachmentOut.model_validate(m.attachment) if m.attachment else None,
        )
        for m in rows
    ]  # newest first


class MediaStatusOut(BaseModel):
    pending: int = 0
    described: int = 0
    failed: int = 0
    skipped: int = 0


@router.get("/chats/{chat_id}/media-status", response_model=MediaStatusOut)
async def media_status(
    chat_id: int, session: AsyncSession = Depends(get_session)
) -> MediaStatusOut:
    """How much of this chat's media has been turned into text — the
    description backlog after a burst of photos or a media-heavy import."""
    await _chat_or_404(session, chat_id)
    rows = (
        await session.execute(
            select(MessageAttachment.status, func.count())
            .where(MessageAttachment.chat_id == chat_id)
            .group_by(MessageAttachment.status)
        )
    ).all()
    return MediaStatusOut(**{status_: n for status_, n in rows})


@router.get("/chats/{chat_id}/search", response_model=list[SearchHitOut])
async def search(
    chat_id: int, q: str, k: int = 5, session: AsyncSession = Depends(get_session)
) -> list[SearchHitOut]:
    await _chat_or_404(session, chat_id)
    embedder = await ensure_embedder(session, "memory")
    hits = await search_chat_history(session, embedder, chat_id, q, k=max(1, min(k, 20)))
    return [SearchHitOut(chunk_id=h.chunk_id, score=h.score, rendered=h.rendered) for h in hits]


@router.post(
    "/chats/{chat_id}/import", response_model=ImportJobOut, status_code=status.HTTP_202_ACCEPTED
)
async def start_import(
    chat_id: int, file: UploadFile, session: AsyncSession = Depends(get_session)
) -> ImportJob:
    await _chat_or_404(session, chat_id)
    running = (
        await session.execute(
            select(ImportJob).where(
                ImportJob.chat_id == chat_id,
                ImportJob.status.in_(("pending", "validating", "ingesting")),
            )
        )
    ).scalars().first()
    if running is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An import is already running for this chat")

    job = ImportJob(chat_id=chat_id, filename=file.filename or "export.json")
    session.add(job)
    await session.commit()

    imports_dir = Path(get_settings().imports_dir)
    imports_dir.mkdir(parents=True, exist_ok=True)
    # A bare result.json or a full export ZIP (with media) — sniffed, not
    # extension-matched, by run_import.
    dest = imports_dir / f"job_{job.id}.upload"
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
        spawn(run_import(get_sessionmaker(), job.id, dest), name=f"import-{job.id}")
    except Exception:
        # A mid-upload disconnect would otherwise strand the job as `pending`
        # forever, and the 409 guard above blocks every future import.
        dest.unlink(missing_ok=True)
        job.status = "failed"
        job.detail = "Upload interrupted before the file arrived"
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()
        raise
    return job


@router.get("/chats/{chat_id}/imports", response_model=list[ImportJobOut])
async def list_imports(
    chat_id: int, session: AsyncSession = Depends(get_session)
) -> list[ImportJob]:
    await _chat_or_404(session, chat_id)
    return list(
        (
            await session.execute(
                select(ImportJob).where(ImportJob.chat_id == chat_id).order_by(ImportJob.id.desc())
            )
        ).scalars()
    )


class RunOut(BaseModel):
    id: int
    trigger: str
    status: str
    request_text: str
    response_text: str | None
    tool_calls: list[dict] | None = None
    error: str | None
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class GlobalRunOut(RunOut):
    chat_id: int
    chat_title: str


@router.get("/runs", response_model=list[GlobalRunOut])
async def recent_runs(
    limit: int = 20, session: AsyncSession = Depends(get_session)
) -> list[GlobalRunOut]:
    """Recent agent runs across all chats — the operator's activity feed."""
    rows = (
        await session.execute(
            select(AgentRun, Chat.title)
            .join(Chat, Chat.id == AgentRun.chat_id)
            .order_by(AgentRun.id.desc())
            .limit(max(1, min(limit, 100)))
        )
    ).all()
    return [
        GlobalRunOut(
            id=run.id,
            trigger=run.trigger,
            status=run.status,
            request_text=run.request_text,
            response_text=run.response_text,
            tool_calls=run.tool_calls,
            error=run.error,
            created_at=run.created_at,
            finished_at=run.finished_at,
            chat_id=run.chat_id,
            chat_title=title or "",
        )
        for run, title in rows
    ]


@router.get("/chats/{chat_id}/runs", response_model=list[RunOut])
async def list_runs(
    chat_id: int, limit: int = 20, session: AsyncSession = Depends(get_session)
) -> list[AgentRun]:
    await _chat_or_404(session, chat_id)
    return list(
        (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.chat_id == chat_id)
                .order_by(AgentRun.id.desc())
                .limit(max(1, min(limit, 100)))
            )
        ).scalars()
    )


class GapOut(BaseModel):
    id: int
    gap_start: datetime
    gap_end: datetime

    model_config = {"from_attributes": True}


@router.get("/chats/{chat_id}/gaps", response_model=list[GapOut])
async def list_gaps(chat_id: int, session: AsyncSession = Depends(get_session)) -> list[MemoryGap]:
    await _chat_or_404(session, chat_id)
    return list(
        (
            await session.execute(
                select(MemoryGap)
                .where(MemoryGap.chat_id == chat_id)
                .order_by(MemoryGap.gap_end.desc())
                .limit(20)
            )
        ).scalars()
    )


class ForgetRequest(BaseModel):
    """Deletes matching messages and rebuilds the chat's memory. Telegram
    never delivers deletion events, so this is the operator's only lever."""

    sender_id: int | None = None
    before: datetime | None = None
    after: datetime | None = None
    everything: bool = False  # also wipe notes


@router.post("/chats/{chat_id}/forget")
async def forget(
    chat_id: int, body: ForgetRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    from app.ingest.history_import import reset_chat_memory

    await _chat_or_404(session, chat_id)
    if not body.everything and body.sender_id is None and body.before is None and body.after is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Specify sender_id, a date range, or everything=true",
        )
    stmt = delete(Message).where(Message.chat_id == chat_id)
    if not body.everything:
        if body.sender_id is not None:
            stmt = stmt.where(Message.sender_id == body.sender_id)
        if body.before is not None:
            stmt = stmt.where(Message.sent_at < body.before)
        if body.after is not None:
            stmt = stmt.where(Message.sent_at > body.after)
    result = await session.execute(stmt)
    await reset_chat_memory(session, chat_id)
    if body.everything:
        await session.execute(delete(Note).where(Note.chat_id == chat_id))
        await session.execute(delete(MemoryGap).where(MemoryGap.chat_id == chat_id))
    await session.commit()
    return {"deleted_messages": result.rowcount or 0}


@router.delete("/imports/{job_id}/messages")
async def remove_import(job_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import not found")
    deleted = await delete_import(session, job)
    await session.commit()
    return {"deleted_messages": deleted}
