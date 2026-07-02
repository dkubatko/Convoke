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
from app.models import (
    Chat,
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
    slots: dict
    status: str
    error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/workflows/{workflow_id}/fires", response_model=list[FireOut])
async def list_fires(
    workflow_id: int, session: AsyncSession = Depends(get_session)
) -> list[PendingFire]:
    return list(
        (
            await session.execute(
                select(PendingFire)
                .where(PendingFire.workflow_id == workflow_id)
                .order_by(PendingFire.id.desc())
                .limit(50)
            )
        ).scalars()
    )
