from datetime import datetime

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, get_sessionmaker
from app.core.security import require_operator
from app.core.tasks import spawn
from app.intent.examples import generate_examples
from app.memory.runtime import ensure_embedder
from app.threads import visible_thread_keys
from sqlalchemy import func

from app.models import (
    AgentRun,
    Chat,
    IntentCursor,
    IntentEpisode,
    Message,
    PendingFire,
    Workflow,
    WorkflowAssignment,
)

router = APIRouter(dependencies=[Depends(require_operator)])


class SlotSpec(BaseModel):
    name: str
    description: str = ""


class WorkflowIn(BaseModel):
    name: str
    type: str  # scheduled | intent
    action_prompt: str
    enabled: bool = True
    cron: str | None = None
    trigger_prompt: str | None = None
    required_slots: list[SlotSpec] = []
    confirm: bool = True
    # 0 = no rate limit. A topic converging during the cooldown parks and is
    # rechecked when it lifts — never dropped. Episode dedup governs re-firing.
    cooldown_seconds: int = 0
    # How long a handled topic stays open as dedup memory (prefilter-gated).
    dedup_window_hours: int = 12
    chat_ids: list[int] = []


class WorkflowOut(BaseModel):
    id: int
    name: str
    type: str
    enabled: bool
    action_prompt: str
    cron: str | None
    next_fire_at: datetime | None
    trigger_prompt: str | None
    required_slots: list
    confirm: bool
    cooldown_seconds: int
    dedup_window_hours: int
    threshold: float | None
    examples_status: str
    chat_ids: list[int]


def _validate(body: WorkflowIn) -> None:
    if body.type not in ("scheduled", "intent"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "type must be scheduled or intent")
    if body.type == "scheduled":
        if not body.cron or not croniter.is_valid(body.cron):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid cron expression")
    if body.type == "intent" and not body.trigger_prompt:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "intent workflows need a trigger_prompt")


async def _out(session: AsyncSession, wf: Workflow) -> WorkflowOut:
    chat_ids = list(
        (
            await session.execute(
                select(WorkflowAssignment.chat_id).where(WorkflowAssignment.workflow_id == wf.id)
            )
        ).scalars()
    )
    return WorkflowOut(
        id=wf.id,
        name=wf.name,
        type=wf.type,
        enabled=wf.enabled,
        action_prompt=wf.action_prompt,
        cron=wf.cron,
        next_fire_at=wf.next_fire_at,
        trigger_prompt=wf.trigger_prompt,
        required_slots=wf.required_slots or [],
        confirm=wf.confirm,
        cooldown_seconds=wf.cooldown_seconds,
        dedup_window_hours=wf.dedup_window_hours,
        threshold=wf.threshold,
        examples_status=wf.examples_status,
        chat_ids=chat_ids,
    )


async def _seed_cursor(session: AsyncSession, workflow_id: int, chat_id: int) -> None:
    """Seed a newly assigned intent workflow's cursor at the chat's current
    tail, so it starts evaluating from 'now' — never the imported backlog."""
    existing = await session.get(IntentCursor, (workflow_id, chat_id, 0))
    if existing is not None:
        return
    tail = (
        await session.execute(
            select(func.max(Message.tg_message_id)).where(Message.chat_id == chat_id)
        )
    ).scalar()
    session.add(
        IntentCursor(
            workflow_id=workflow_id, chat_id=chat_id, thread_key=0,
            last_tg_message_id=tail or 0,
        )
    )


async def _set_assignments(session: AsyncSession, wf: Workflow, chat_ids: list[int]) -> None:
    await session.execute(
        delete(WorkflowAssignment).where(WorkflowAssignment.workflow_id == wf.id)
    )
    for cid in set(chat_ids):
        if await session.get(Chat, cid) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown chat id {cid}")
        session.add(WorkflowAssignment(workflow_id=wf.id, chat_id=cid))
        if wf.type == "intent":
            await _seed_cursor(session, wf.id, cid)


def _apply(wf: Workflow, body: WorkflowIn) -> bool:
    """Returns True when the intent trigger changed (examples need regen)."""
    trigger_changed = wf.trigger_prompt != body.trigger_prompt or (wf.required_slots or []) != [
        s.model_dump() for s in body.required_slots
    ]
    wf.name = body.name
    wf.type = body.type
    wf.enabled = body.enabled
    wf.action_prompt = body.action_prompt
    wf.cron = body.cron
    wf.trigger_prompt = body.trigger_prompt
    wf.required_slots = [s.model_dump() for s in body.required_slots]
    wf.confirm = body.confirm
    wf.cooldown_seconds = body.cooldown_seconds
    wf.dedup_window_hours = body.dedup_window_hours
    if wf.type == "scheduled":
        wf.next_fire_at = None  # recomputed from cron on the next tick
    return wf.type == "intent" and trigger_changed


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: WorkflowIn, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    _validate(body)
    wf = Workflow(name=body.name, type=body.type, action_prompt=body.action_prompt)
    session.add(wf)
    await session.flush()
    needs_examples = _apply(wf, body)
    await _set_assignments(session, wf, body.chat_ids)
    await session.commit()
    if needs_examples:
        spawn(generate_examples(get_sessionmaker(), await ensure_embedder(session), wf.id), name=f"examples-{wf.id}")
    return await _out(session, wf)


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(session: AsyncSession = Depends(get_session)) -> list[WorkflowOut]:
    rows = (await session.execute(select(Workflow).order_by(Workflow.id))).scalars().all()
    return [await _out(session, wf) for wf in rows]


@router.put("/workflows/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: int, body: WorkflowIn, session: AsyncSession = Depends(get_session)
) -> WorkflowOut:
    _validate(body)
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    needs_examples = _apply(wf, body)
    if needs_examples:
        wf.examples_status = "pending"
    await _set_assignments(session, wf, body.chat_ids)
    await session.commit()
    if needs_examples:
        spawn(generate_examples(get_sessionmaker(), await ensure_embedder(session), wf.id), name=f"examples-{wf.id}")
    return await _out(session, wf)


@router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(workflow_id: int, session: AsyncSession = Depends(get_session)) -> None:
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    await session.delete(wf)
    await session.commit()


class FireOut(BaseModel):
    id: int
    workflow_id: int
    chat_id: int
    chat_title: str = ""
    slots: dict
    status: str
    error: str | None
    # Links a done fire to the agent run it queued, so UIs can merge the two
    # into one activity entry instead of showing the same event twice.
    agent_run_id: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/workflows/{workflow_id}/fires", response_model=list[FireOut])
async def list_fires(
    workflow_id: int, session: AsyncSession = Depends(get_session)
) -> list[FireOut]:
    rows = (
        await session.execute(
            select(PendingFire, Chat.title)
            .join(Chat, Chat.id == PendingFire.chat_id)
            .where(PendingFire.workflow_id == workflow_id)
            .order_by(PendingFire.id.desc())
            .limit(50)
        )
    ).all()
    return [
        FireOut(
            id=f.id, workflow_id=f.workflow_id, chat_id=f.chat_id, chat_title=title or "",
            slots=f.slots or {}, status=f.status, error=f.error,
            agent_run_id=f.agent_run_id, created_at=f.created_at,
        )
        for f, title in rows
    ]


# ---------- per-chat observability + control ----------


class CursorOut(BaseModel):
    """Where the detector's last check of one thread ended."""

    thread_key: int
    last_tg_message_id: int
    last_evaluated_at: datetime | None
    last_stage: str | None
    last_score: float | None
    last_confidence: float | None

    model_config = {"from_attributes": True}


class EpisodeOut(BaseModel):
    """One tracked occurrence of the intent — the unit the UI renders."""

    id: int
    thread_key: int
    status: str
    summary: str | None
    slots: dict
    confidence: float | None
    execution_summary: str | None
    close_reason: str | None
    opened_at: datetime
    last_activity_at: datetime
    fired_at: datetime | None
    closed_at: datetime | None

    model_config = {"from_attributes": True}


async def _episodes_for(
    session: AsyncSession, workflow_id: int, chat_id: int, limit: int = 10
) -> list[EpisodeOut]:
    # Only threads that actually exist AND are monitored surface here. This
    # excludes disabled threads and orphaned cursors/episodes (whose messages
    # were deleted or re-imported) — both would otherwise show as phantom
    # threads. Re-enabling a thread or new messages bring rows back; nothing is
    # deleted, only filtered.
    allowed = await visible_thread_keys(session, chat_id)
    if not allowed:
        return []
    rows = (
        (
            await session.execute(
                select(IntentEpisode)
                .where(
                    IntentEpisode.workflow_id == workflow_id,
                    IntentEpisode.chat_id == chat_id,
                    IntentEpisode.thread_key.in_(sorted(allowed)),
                )
                .order_by(
                    # open episodes first, then most recent activity
                    (IntentEpisode.status == "closed").asc(),
                    IntentEpisode.last_activity_at.desc(),
                    IntentEpisode.id.desc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [EpisodeOut.model_validate(e) for e in rows]


async def _cursors_for(
    session: AsyncSession, workflow_id: int, chat_id: int
) -> list[CursorOut]:
    allowed = await visible_thread_keys(session, chat_id)
    if not allowed:
        return []
    rows = (
        (
            await session.execute(
                select(IntentCursor)
                .where(
                    IntentCursor.workflow_id == workflow_id,
                    IntentCursor.chat_id == chat_id,
                    IntentCursor.thread_key.in_(sorted(allowed)),
                )
                .order_by(IntentCursor.thread_key)
            )
        )
        .scalars()
        .all()
    )
    return [CursorOut.model_validate(c) for c in rows]


async def _pending_for(
    session: AsyncSession, chat_id: int, cursors: list["CursorOut"]
) -> int:
    """Non-self messages a workflow hasn't evaluated, counted PER THREAD against
    that thread's own cursor. A single global min-cursor miscounts: a stale seed
    cursor in a quiet thread would flag another thread's already-evaluated
    messages as pending forever."""
    total = 0
    for c in cursors:
        thread_pred = (
            Message.thread_id.is_(None)
            if c.thread_key == 0
            else Message.thread_id == c.thread_key
        )
        total += (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.source != "self",
                    Message.tg_message_id > c.last_tg_message_id,
                    thread_pred,
                )
            )
        ).scalar() or 0
    return total


class ChatRunOut(BaseModel):
    id: int
    status: str
    error: str | None
    response_text: str | None
    tool_calls: list[dict] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatWorkflowOut(BaseModel):
    id: int
    name: str
    type: str
    enabled: bool
    confirm: bool
    cooldown_seconds: int
    dedup_window_hours: int
    threshold: float | None
    examples_status: str
    cron: str | None
    next_fire_at: datetime | None
    trigger_prompt: str | None
    action_prompt: str
    required_slots: list
    assigned: bool
    cursors: list[CursorOut]
    episodes: list[EpisodeOut]
    recent_fires: list[FireOut]
    recent_runs: list[ChatRunOut]
    # Messages THIS workflow hasn't evaluated yet (past its own cursor).
    pending_messages: int = 0


@router.get("/chats/{chat_id}/workflows", response_model=list[ChatWorkflowOut])
async def chat_workflows(
    chat_id: int, session: AsyncSession = Depends(get_session)
) -> list[ChatWorkflowOut]:
    """Every workflow, with this chat's assignment, live trigger state, and
    recent activity — the per-chat observability panel."""
    chat = await session.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    assigned_ids = set(
        (
            await session.execute(
                select(WorkflowAssignment.workflow_id).where(WorkflowAssignment.chat_id == chat_id)
            )
        ).scalars()
    )
    workflows = (await session.execute(select(Workflow).order_by(Workflow.id))).scalars().all()

    out: list[ChatWorkflowOut] = []
    for wf in workflows:
        cursors = await _cursors_for(session, wf.id, chat_id)
        episodes = (
            await _episodes_for(session, wf.id, chat_id) if wf.type == "intent" else []
        )
        # Messages THIS workflow hasn't evaluated yet: newer than its own
        # least-advanced cursor. Only meaningful for an assigned intent
        # workflow with a seeded cursor (no cursor yet → nothing counted;
        # seeding starts it at the chat tail anyway).
        pending_messages = 0
        if wf.type == "intent" and wf.id in assigned_ids and cursors:
            pending_messages = await _pending_for(session, chat_id, cursors)
        fires = (
            (
                await session.execute(
                    select(PendingFire)
                    .where(PendingFire.workflow_id == wf.id, PendingFire.chat_id == chat_id)
                    .order_by(PendingFire.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        runs = (
            (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.workflow_id == wf.id, AgentRun.chat_id == chat_id)
                    .order_by(AgentRun.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        out.append(
            ChatWorkflowOut(
                id=wf.id,
                name=wf.name,
                type=wf.type,
                enabled=wf.enabled,
                confirm=wf.confirm,
                cooldown_seconds=wf.cooldown_seconds,
                dedup_window_hours=wf.dedup_window_hours,
                threshold=wf.threshold,
                examples_status=wf.examples_status,
                cron=wf.cron,
                next_fire_at=wf.next_fire_at,
                trigger_prompt=wf.trigger_prompt,
                action_prompt=wf.action_prompt,
                required_slots=wf.required_slots or [],
                assigned=wf.id in assigned_ids,
                cursors=cursors,
                episodes=episodes,
                recent_fires=[
                    FireOut(
                        id=f.id, workflow_id=f.workflow_id, chat_id=f.chat_id,
                        chat_title=chat.title or "", slots=f.slots or {}, status=f.status,
                        error=f.error, agent_run_id=f.agent_run_id, created_at=f.created_at,
                    )
                    for f in fires
                ],
                recent_runs=[ChatRunOut.model_validate(r) for r in runs],
                pending_messages=pending_messages,
            )
        )
    return out


class WorkflowChatOut(BaseModel):
    """One chat's live state for a workflow — the workflow-centric mirror of
    ChatWorkflowOut, powering the workflow detail page."""

    chat_id: int
    chat_title: str
    chat_status: str
    cursors: list[CursorOut]
    episodes: list[EpisodeOut]
    pending_messages: int
    recent_fires: list[FireOut]
    recent_runs: list[ChatRunOut]


class WorkflowDetailOut(WorkflowOut):
    chats: list[WorkflowChatOut]


@router.get("/workflows/{workflow_id}/detail", response_model=WorkflowDetailOut)
async def workflow_detail(
    workflow_id: int, session: AsyncSession = Depends(get_session)
) -> WorkflowDetailOut:
    """A workflow with every chat it's assigned to and that chat's live state
    and recent activity — the workflow-centric observability panel."""
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    base = await _out(session, wf)

    chats = (
        (
            await session.execute(
                select(Chat)
                .join(WorkflowAssignment, WorkflowAssignment.chat_id == Chat.id)
                .where(WorkflowAssignment.workflow_id == workflow_id)
                .order_by(Chat.id)
            )
        )
        .scalars()
        .all()
    )

    chat_out: list[WorkflowChatOut] = []
    for chat in chats:
        cursors = await _cursors_for(session, workflow_id, chat.id)
        episodes = (
            await _episodes_for(session, workflow_id, chat.id) if wf.type == "intent" else []
        )
        pending = 0
        if wf.type == "intent" and cursors:
            pending = await _pending_for(session, chat.id, cursors)
        fires = (
            (
                await session.execute(
                    select(PendingFire)
                    .where(PendingFire.workflow_id == workflow_id, PendingFire.chat_id == chat.id)
                    .order_by(PendingFire.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        runs = (
            (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.workflow_id == workflow_id, AgentRun.chat_id == chat.id)
                    .order_by(AgentRun.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        chat_out.append(
            WorkflowChatOut(
                chat_id=chat.id,
                chat_title=chat.title or str(chat.tg_chat_id),
                chat_status=chat.status,
                cursors=cursors,
                episodes=episodes,
                pending_messages=pending,
                recent_fires=[
                    FireOut(
                        id=f.id, workflow_id=f.workflow_id, chat_id=f.chat_id,
                        chat_title=chat.title or "", slots=f.slots or {}, status=f.status,
                        error=f.error, agent_run_id=f.agent_run_id, created_at=f.created_at,
                    )
                    for f in fires
                ],
                recent_runs=[ChatRunOut.model_validate(r) for r in runs],
            )
        )
    return WorkflowDetailOut(**base.model_dump(), chats=chat_out)


@router.put("/chats/{chat_id}/workflows", response_model=list[int])
async def set_chat_workflows(
    chat_id: int, workflow_ids: list[int], session: AsyncSession = Depends(get_session)
) -> list[int]:
    """Per-chat assignment control, symmetric with the per-workflow chat list."""
    if await session.get(Chat, chat_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    await session.execute(
        delete(WorkflowAssignment).where(WorkflowAssignment.chat_id == chat_id)
    )
    for wid in set(workflow_ids):
        wf = await session.get(Workflow, wid)
        if wf is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown workflow id {wid}")
        session.add(WorkflowAssignment(workflow_id=wid, chat_id=chat_id))
        if wf.type == "intent":
            await _seed_cursor(session, wid, chat_id)
    await session.commit()
    return sorted(set(workflow_ids))
