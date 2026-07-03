"""Scheduled workflows: next_fire_at + croniter + a small tick loop.

Deliberately not APScheduler: 4.x is still pre-release, 3.x pickles jobs into
its jobstore. A next_fire_at column is DB-native, restart-safe and trivially
inspectable from the UI.
"""

import asyncio
import logging
from datetime import datetime, timezone

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentRun, Chat, Workflow, WorkflowAssignment

log = logging.getLogger("convoke.scheduler")

TICK_S = 15


def next_fire(cron: str, after: datetime) -> datetime:
    return croniter(cron, after).get_next(datetime)


class ScheduleLoop:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — loop must survive
                log.exception("schedule tick failed")
            await asyncio.sleep(TICK_S)

    async def tick(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        fired = 0
        async with self.sessionmaker() as session:
            workflows = (
                (
                    await session.execute(
                        select(Workflow).where(
                            Workflow.type == "scheduled", Workflow.enabled.is_(True)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for wf in workflows:
                if not wf.cron:
                    continue
                if wf.next_fire_at is None:
                    wf.next_fire_at = next_fire(wf.cron, now)
                    continue
                due_at = wf.next_fire_at
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
                if due_at > now:
                    continue
                fired += await self._fire(session, wf)
                wf.next_fire_at = next_fire(wf.cron, now)
            await session.commit()
        return fired

    async def _fire(self, session: AsyncSession, wf: Workflow) -> int:
        chat_ids = (
            (
                await session.execute(
                    select(WorkflowAssignment.chat_id)
                    .join(Chat, Chat.id == WorkflowAssignment.chat_id)
                    .where(
                        WorkflowAssignment.workflow_id == wf.id,
                        Chat.status == "authorized",
                    )
                )
            )
            .scalars()
            .all()
        )
        for chat_id in chat_ids:
            session.add(
                AgentRun(
                    chat_id=chat_id,
                    trigger="workflow",
                    workflow_id=wf.id,
                    request_text=wf.action_prompt,
                )
            )
        if chat_ids:
            log.info("scheduled workflow %s fired for %d chat(s)", wf.id, len(chat_ids))
        return len(chat_ids)
