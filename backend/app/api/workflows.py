import asyncio
from datetime import datetime

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session, get_sessionmaker
from app.core.security import require_operator
from app.intent.examples import generate_examples
from app.memory.runtime import get_embedder
from sqlalchemy import func

from app.models import (
    AgentRun,
    Chat,
    ChatEvalState,
    Message,
    PendingFire,
    TriggerState,
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
    cooldown_seconds: int = 3600
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
        threshold=wf.threshold,
        examples_status=wf.examples_status,
        chat_ids=chat_ids,
    )


async def _set_assignments(session: AsyncSession, wf: Workflow, chat_ids: list[int]) -> None:
    await session.execute(
        delete(WorkflowAssignment).where(WorkflowAssignment.workflow_id == wf.id)
    )
    for cid in set(chat_ids):
        if await session.get(Chat, cid) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown chat id {cid}")
        session.add(WorkflowAssignment(workflow_id=wf.id, chat_id=cid))


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
        asyncio.create_task(generate_examples(get_sessionmaker(), get_embedder(), wf.id))
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
        asyncio.create_task(generate_examples(get_sessionmaker(), get_embedder(), wf.id))
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
            slots=f.slots or {}, status=f.status, error=f.error, created_at=f.created_at,
        )
        for f, title in rows
    ]


# ---------- per-chat observability + control ----------


class TriggerStateOut(BaseModel):
    thread_key: int
    slots: dict
    last_evaluated_at: datetime | None
    last_stage: str | None
    last_score: float | None
    last_confidence: float | None
    last_match_at: datetime | None
    cooldown_until: datetime | None

    model_config = {"from_attributes": True}


class ChatRunOut(BaseModel):
    id: int
    status: str
    error: str | None
    response_text: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatWorkflowOut(BaseModel):
    id: int
    name: str
    type: str
    enabled: bool
    confirm: bool
    threshold: float | None
    examples_status: str
    cron: str | None
    next_fire_at: datetime | None
    required_slots: list
    assigned: bool
    states: list[TriggerStateOut]
    recent_fires: list[FireOut]
    recent_runs: list[ChatRunOut]
    # Messages newer than the evaluation cursor — i.e. waiting for the next
    # window to close. Same value for every workflow of the chat.
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
    eval_state = await session.get(ChatEvalState, chat_id)
    cursor = eval_state.last_tg_message_id if eval_state else 0
    pending_messages = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.chat_id == chat_id,
                Message.tg_message_id > cursor,
                Message.source != "self",
            )
        )
    ).scalar() or 0
    workflows = (await session.execute(select(Workflow).order_by(Workflow.id))).scalars().all()

    out: list[ChatWorkflowOut] = []
    for wf in workflows:
        states = (
            (
                await session.execute(
                    select(TriggerState)
                    .where(TriggerState.workflow_id == wf.id, TriggerState.chat_id == chat_id)
                    .order_by(TriggerState.thread_key)
                )
            )
            .scalars()
            .all()
        )
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
                threshold=wf.threshold,
                examples_status=wf.examples_status,
                cron=wf.cron,
                next_fire_at=wf.next_fire_at,
                required_slots=wf.required_slots or [],
                assigned=wf.id in assigned_ids,
                states=[TriggerStateOut.model_validate(s) for s in states],
                recent_fires=[
                    FireOut(
                        id=f.id, workflow_id=f.workflow_id, chat_id=f.chat_id,
                        chat_title=chat.title or "", slots=f.slots or {}, status=f.status,
                        error=f.error, created_at=f.created_at,
                    )
                    for f in fires
                ],
                recent_runs=[ChatRunOut.model_validate(r) for r in runs],
                pending_messages=pending_messages,
            )
        )
    return out


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
        if await session.get(Workflow, wid) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown workflow id {wid}")
        session.add(WorkflowAssignment(workflow_id=wid, chat_id=chat_id))
    await session.commit()
    return sorted(set(workflow_ids))
