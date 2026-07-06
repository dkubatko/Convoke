from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session, get_sessionmaker
from app.core.tasks import spawn
from app.core.security import require_operator
from app.ingest.history_import import delete_import, run_import
from app.media.render import message_body
from app.memory.chunker import resolve_reply_targets
from app.memory.runtime import ensure_embedder
from app.memory.store import search_chat_history
from app.models import (
    AgentRun,
    Chat,
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
    distance: float
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
    title: str | None = None  # "" or whitespace clears back to the default


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
                    sender_name=p.sender_name,
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
        row = ChatThread(chat_id=chat_id, thread_key=thread_key)
        session.add(row)
    if body.monitored is not None:
        row.monitored = body.monitored
    if body.title is not None:
        row.title = body.title.strip() or None  # blank clears back to the default
    await session.commit()
    return await _list_threads(session, chat_id, 5)


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

    def preview(m: Message) -> ReplyPreview | None:
        target = targets.get(m.reply_to_tg_message_id or 0)
        if target is None:
            return None
        text = message_body(target).replace("\n", " ")
        return ReplyPreview(
            sender_name=target.sender_name or "Unknown",
            text=text[:140] + ("…" if len(text) > 140 else ""),
        )

    return [
        MessageOut(
            id=m.id, tg_message_id=m.tg_message_id, sender_name=m.sender_name,
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
    embedder = await ensure_embedder(session)
    hits = await search_chat_history(session, embedder, chat_id, q, k=max(1, min(k, 20)))
    return [SearchHitOut(chunk_id=h.chunk_id, distance=h.distance, rendered=h.rendered) for h in hits]


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
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)

    spawn(run_import(get_sessionmaker(), job.id, dest), name=f"import-{job.id}")
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
