"""Per-chat Identity and Agent tasks — the chat page's tabs for who the bot is
in a chat and what it has scheduled there."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.identity import (
    DEFAULT_IDENTITY,
    MAX_IDENTITY_CHARS,
    MAX_NAME_CHARS,
    MAX_NAMES,
    PLACEHOLDERS,
    normalize_names,
)
from app.api.workflows import ChatRunOut
from app.core.db import get_session
from app.core.security import require_operator
from app.models import AgentRun, AgentTask, Chat
from app.scheduler.tasks import cron_next, utc

router = APIRouter(dependencies=[Depends(require_operator)])

RECENT_RUNS = 10


async def _chat(session: AsyncSession, chat_id: int) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    return chat


# --- identity -------------------------------------------------------------------


class IdentityOut(BaseModel):
    identity: str  # effective text: the custom one, or the default
    default: str
    customized: bool
    placeholders: dict[str, str]
    max_chars: int
    # Names members call the bot by; a whole-word mention invokes it.
    names: list[str]
    max_names: int


class IdentityIn(BaseModel):
    # None / blank / identical to the default → back to the default.
    identity: str | None = Field(default=None, max_length=MAX_IDENTITY_CHARS)
    # None = leave the names unchanged.
    names: list[str] | None = Field(default=None, max_length=MAX_NAMES)


def _identity_out(chat: Chat) -> IdentityOut:
    return IdentityOut(
        identity=chat.identity or DEFAULT_IDENTITY,
        default=DEFAULT_IDENTITY,
        customized=chat.identity is not None,
        placeholders=PLACEHOLDERS,
        max_chars=MAX_IDENTITY_CHARS,
        names=chat.call_names or [],
        max_names=MAX_NAMES,
    )


@router.get("/chats/{chat_id}/identity", response_model=IdentityOut)
async def get_identity(chat_id: int, session: AsyncSession = Depends(get_session)) -> IdentityOut:
    return _identity_out(await _chat(session, chat_id))


@router.put("/chats/{chat_id}/identity", response_model=IdentityOut)
async def set_identity(
    chat_id: int, body: IdentityIn, session: AsyncSession = Depends(get_session)
) -> IdentityOut:
    chat = await _chat(session, chat_id)
    text = (body.identity or "").strip()
    # Storing the default verbatim would freeze it: a later improvement to
    # DEFAULT_IDENTITY must still reach chats that never customized.
    chat.identity = text if text and text != DEFAULT_IDENTITY.strip() else None
    if body.names is not None:
        names = normalize_names(body.names)
        too_long = [n for n in names if len(n) > MAX_NAME_CHARS]
        if too_long:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Names can be at most {MAX_NAME_CHARS} characters: {too_long[0]!r}",
            )
        chat.call_names = names or None
    await session.commit()
    return _identity_out(chat)


# --- agent tasks ----------------------------------------------------------------


class AgentTaskOut(BaseModel):
    id: int
    task: str
    kind: str  # once | recurring
    cron: str | None
    timezone: str | None
    depth: int
    run_at: datetime | None
    ends_at: datetime | None
    next_fire_at: datetime | None
    enabled: bool
    status: str
    thread_id: int | None
    created_by_name: str
    created_at: datetime
    last_fired_at: datetime | None
    recent_runs: list[ChatRunOut]


class AgentTaskPatch(BaseModel):
    enabled: bool


async def _task_out(session: AsyncSession, t: AgentTask) -> AgentTaskOut:
    runs = (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.task_id == t.id)
            .order_by(AgentRun.id.desc())
            .limit(RECENT_RUNS)
        )
    ).scalars().all()
    return AgentTaskOut(
        id=t.id,
        task=t.task,
        kind="recurring" if t.cron else "once",
        cron=t.cron,
        timezone=t.timezone,
        depth=t.depth,
        run_at=t.run_at,
        ends_at=t.ends_at,
        next_fire_at=t.next_fire_at,
        enabled=t.enabled,
        status=t.status,
        thread_id=t.thread_id,
        created_by_name=t.created_by_name,
        created_at=t.created_at,
        last_fired_at=t.last_fired_at,
        recent_runs=[ChatRunOut.model_validate(r) for r in runs],
    )


async def _task(session: AsyncSession, chat_id: int, task_id: int) -> AgentTask:
    t = await session.get(AgentTask, task_id)
    if t is None or t.chat_id != chat_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return t


@router.get("/chats/{chat_id}/tasks", response_model=list[AgentTaskOut])
async def list_tasks(chat_id: int, session: AsyncSession = Depends(get_session)) -> list[AgentTaskOut]:
    await _chat(session, chat_id)
    rows = (
        await session.execute(
            select(AgentTask).where(AgentTask.chat_id == chat_id).order_by(AgentTask.id.desc())
        )
    ).scalars().all()
    return [await _task_out(session, t) for t in rows]


@router.patch("/chats/{chat_id}/tasks/{task_id}", response_model=AgentTaskOut)
async def update_task(
    chat_id: int, task_id: int, body: AgentTaskPatch, session: AsyncSession = Depends(get_session)
) -> AgentTaskOut:
    """Pause or resume. Resuming recomputes a recurring task's next fire from
    now (no burst of catch-up runs); a one-off whose time passed can't resume."""
    t = await _task(session, chat_id, task_id)
    if body.enabled and not t.enabled and t.status == "active":
        now = datetime.now(timezone.utc)
        if t.cron:
            nxt = cron_next(t.cron, now, t.timezone)
            ends_at = utc(t.ends_at)
            if ends_at is not None and nxt > ends_at:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This task's end date has passed.")
            t.next_fire_at = nxt
        elif utc(t.run_at) is not None and utc(t.run_at) <= now:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Its time has already passed — ask the bot to schedule it again.",
            )
    t.enabled = body.enabled
    await session.commit()
    return await _task_out(session, t)


@router.delete("/chats/{chat_id}/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(chat_id: int, task_id: int, session: AsyncSession = Depends(get_session)) -> None:
    t = await _task(session, chat_id, task_id)
    await session.delete(t)
    await session.commit()
