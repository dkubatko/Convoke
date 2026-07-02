import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session, get_sessionmaker
from app.core.security import require_operator
from app.ingest.history_import import delete_import, run_import
from app.memory.runtime import get_embedder
from app.memory.store import search_chat_history
from app.models import Chat, ImportJob, Message

router = APIRouter(dependencies=[Depends(require_operator)])


class MessageOut(BaseModel):
    id: int
    tg_message_id: int
    sender_name: str
    text: str
    sent_at: datetime
    source: str

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


@router.get("/chats/{chat_id}/messages", response_model=list[MessageOut])
async def recent_messages(
    chat_id: int, limit: int = 50, session: AsyncSession = Depends(get_session)
) -> list[Message]:
    await _chat_or_404(session, chat_id)
    rows = (
        (
            await session.execute(
                select(Message)
                .where(Message.chat_id == chat_id)
                .order_by(Message.tg_message_id.desc())
                .limit(min(limit, 200))
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


@router.get("/chats/{chat_id}/search", response_model=list[SearchHitOut])
async def search(
    chat_id: int, q: str, k: int = 5, session: AsyncSession = Depends(get_session)
) -> list[SearchHitOut]:
    await _chat_or_404(session, chat_id)
    hits = await search_chat_history(session, get_embedder(), chat_id, q, k=min(k, 20))
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
    dest = imports_dir / f"job_{job.id}.json"
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)

    asyncio.create_task(run_import(get_sessionmaker(), job.id, dest))
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


@router.delete("/imports/{job_id}/messages")
async def remove_import(job_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import not found")
    deleted = await delete_import(session, job)
    await session.commit()
    return {"deleted_messages": deleted}
