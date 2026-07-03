"""Intent sweeper: stages 0–3 of the trigger pipeline.

Stage 0 — free gates: only authorized chats with assigned, enabled intent
workflows; windows close on a lull or at N messages; per-state cooldowns and
an LLM-call circuit breaker.
Stage 1 — embedding prefilter against generated example utterances (loose).
Stage 2 — cheap-LLM structured classification with slot updates/retractions.
Stage 3 — persisted convergence state; on convergence, write a PendingFire.
"""

import logging
from datetime import datetime, timedelta, timezone

from pydantic_ai import Agent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import ProviderNotConfigured, build_model, get_provider
from app.core.config import get_settings
from app.intent.examples import dot, load_positive_vectors
from app.intent.schemas import IntentVerdict
from app.intent.state import apply_verdict, decay_state, is_converged, render_slots
from app.memory.chunker import render_message
from app.memory.embeddings import Embedder
from app.models import (
    Chat,
    Message,
    PendingFire,
    TriggerState,
    Workflow,
    WorkflowAssignment,
)

log = logging.getLogger("convoke.intent")

CLASSIFY_PROMPT = """\
You watch a group chat for this intent:
"{trigger_prompt}"

Slots to extract as the conversation converges:
{slots_desc}

Currently accumulated slot state from earlier messages:
{current_slots}

New conversation window:
{window}

Decide whether this window advances the intent, and extract slot updates.
Emit a retraction (value=null) when the group walks back a previous value
("actually let's do Wednesday instead"). Only extract values members actually
converged on, not proposals still under discussion.
"""


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class IntentSweeper:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self.settings = get_settings()

    async def sweep(self, now: datetime | None = None) -> int:
        """One pass over all eligible chats; returns number of windows evaluated."""
        now = now or datetime.now(timezone.utc)
        evaluated = 0
        async with self.sessionmaker() as session:
            chat_ids = (
                (
                    await session.execute(
                        select(Chat.id)
                        .join(WorkflowAssignment, WorkflowAssignment.chat_id == Chat.id)
                        .join(Workflow, Workflow.id == WorkflowAssignment.workflow_id)
                        .where(
                            Chat.status == "authorized",
                            Workflow.type == "intent",
                            Workflow.enabled.is_(True),
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
        for chat_id in chat_ids:
            evaluated += await self._sweep_chat(chat_id, now)
        return evaluated

    async def _sweep_chat(self, chat_id: int, now: datetime) -> int:
        async with self.sessionmaker() as session:
            workflows = (
                (
                    await session.execute(
                        select(Workflow)
                        .join(WorkflowAssignment, WorkflowAssignment.workflow_id == Workflow.id)
                        .where(
                            WorkflowAssignment.chat_id == chat_id,
                            Workflow.type == "intent",
                            Workflow.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not workflows:
                return 0
            wf_ids = [w.id for w in workflows]

            # Each workflow has its OWN cursor (trigger_states.last_tg_message_id).
            states = (
                (
                    await session.execute(
                        select(TriggerState).where(
                            TriggerState.workflow_id.in_(wf_ids),
                            TriggerState.chat_id == chat_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            state_by_key: dict[tuple[int, int], TriggerState] = {
                (s.workflow_id, s.thread_key): s for s in states
            }
            # Load from the least-advanced cursor across all workflows (0 if any
            # is new), then let each evaluate only what IT hasn't seen.
            min_cursor = 0
            for wf in workflows:
                cursors = [s.last_tg_message_id for s in states if s.workflow_id == wf.id]
                min_cursor = min(min_cursor, min(cursors) if cursors else 0)

            rows = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.chat_id == chat_id,
                            Message.tg_message_id > min_cursor,
                            Message.source != "self",  # never trigger on our own replies
                        )
                        .order_by(Message.tg_message_id.desc())
                        .limit(300)
                    )
                )
                .scalars()
                .all()
            )
            messages = list(reversed(rows))  # oldest→newest tail
            if not messages:
                return 0

            # Partition by thread (forum topics interleave unrelated talk).
            by_thread: dict[int, list[Message]] = {}
            for m in messages:
                by_thread.setdefault(m.thread_id or 0, []).append(m)

            max_win = self.settings.intent_window_max_messages
            lull = self.settings.intent_lull_seconds
            evaluated = 0
            for wf in workflows:
                for thread_key, tmsgs in by_thread.items():
                    ts = state_by_key.get((wf.id, thread_key))
                    cursor = ts.last_tg_message_id if ts else 0
                    unevaluated = [m for m in tmsgs if m.tg_message_id > cursor]
                    if not unevaluated:
                        continue
                    last_at = _as_utc(unevaluated[-1].sent_at)
                    if len(unevaluated) < max_win and (now - last_at).total_seconds() < lull:
                        continue  # window still open
                    window = unevaluated[-max_win:]
                    context = await self._prior_context(session, chat_id, thread_key, window[0])
                    stage = await self._evaluate(
                        session, wf, chat_id, thread_key, window, context, now
                    )
                    if stage not in ("cooldown", "prefilter_skip", "throttled"):
                        evaluated += 1
            await session.commit()
            return evaluated

    async def _prior_context(
        self, session: AsyncSession, chat_id: int, thread_key: int, first: Message
    ) -> list[Message]:
        rows = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.chat_id == chat_id,
                        Message.tg_message_id < first.tg_message_id,
                        Message.thread_id.is_(None) if thread_key == 0 else Message.thread_id == thread_key,
                    )
                    .order_by(Message.tg_message_id.desc())
                    .limit(self.settings.intent_context_messages)
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))

    async def _evaluate(
        self,
        session: AsyncSession,
        wf: Workflow,
        chat_id: int,
        thread_key: int,
        window: list[Message],
        context: list[Message],
        now: datetime,
    ) -> str:
        tstate = (
            await session.execute(
                select(TriggerState).where(
                    TriggerState.workflow_id == wf.id,
                    TriggerState.chat_id == chat_id,
                    TriggerState.thread_key == thread_key,
                )
            )
        ).scalar_one_or_none()
        if tstate is None:
            tstate = TriggerState(workflow_id=wf.id, chat_id=chat_id, thread_key=thread_key)
            session.add(tstate)

        def mark(stage: str, score: float | None = None, confidence: float | None = None) -> None:
            tstate.last_evaluated_at = now
            tstate.last_stage = stage
            tstate.last_score = score
            tstate.last_confidence = confidence

        def advance() -> None:
            # Consume the window: this workflow won't re-see these messages.
            tstate.last_tg_message_id = window[-1].tg_message_id

        # Cheap gates first — no embedding, and cursor is NOT advanced, so a
        # workflow in cooldown re-evaluates these messages once it lifts.
        cooldown_until = _as_utc(tstate.cooldown_until)
        if cooldown_until is not None and cooldown_until > now:
            mark("cooldown")
            return "cooldown"
        last_llm = _as_utc(tstate.last_llm_at)
        if (
            last_llm is not None
            and (now - last_llm).total_seconds() < self.settings.intent_min_llm_interval_seconds
        ):
            mark("throttled")
            return "throttled"

        # Stage 1: prefilter (skipped mid-accumulation — follow-ups like "ok
        # wednesday then" may not resemble the examples). Embed each message's
        # raw text (the space the examples were calibrated in) and score the
        # best (message, example) pair: one on-intent message is enough.
        score: float | None = None
        if not tstate.slots:
            positives = await load_positive_vectors(session, wf.id)
            if positives:
                message_vecs = await self.embedder.embed_passages([m.text for m in window])
                score = max(dot(v, p) for v in message_vecs for p in positives)
                if score < (wf.threshold or 0.8):
                    mark("prefilter_skip", score=score)
                    advance()
                    return "prefilter_skip"

        # Stage 2: classifier. The trailing context lets messages that trickle
        # in after a burst ("what do you think?") be judged in situ.
        rendered = "\n".join(render_message(m) for m in [*context, *window])
        verdict = await self._classify(session, wf, tstate, rendered)
        tstate.last_llm_at = now
        if verdict is None:
            mark("classifier_error", score=score)  # no advance — retry when the model is back
            return "classifier_error"

        slots = decay_state(
            dict(tstate.slots or {}),
            _as_utc(tstate.last_match_at),
            now,
            timedelta(hours=self.settings.intent_state_ttl_hours),
        )
        slots = apply_verdict(slots, verdict, now, window[-1].tg_message_id)
        tstate.slots = slots
        if verdict.match:
            tstate.last_match_at = now
        advance()

        if (
            verdict.match
            and verdict.confidence >= 0.7
            and is_converged(wf.required_slots or [], slots)
        ):
            session.add(
                PendingFire(
                    workflow_id=wf.id,
                    chat_id=chat_id,
                    thread_key=thread_key,
                    slots=slots,
                    status="pending",
                )
            )
            tstate.slots = {}
            tstate.cooldown_until = now + timedelta(seconds=wf.cooldown_seconds)
            mark("fired", score=score, confidence=verdict.confidence)
            log.info("workflow %s converged in chat %s: %s", wf.id, chat_id, render_slots(slots))
            await session.commit()
            return "fired"
        elif verdict.match:
            mark("accumulating", score=score, confidence=verdict.confidence)
            return "accumulating"
        else:
            mark("no_match", score=score, confidence=verdict.confidence)
            return "no_match"

    async def _classify(
        self, session: AsyncSession, wf: Workflow, tstate: TriggerState, rendered: str
    ) -> IntentVerdict | None:
        try:
            provider = await get_provider(session, "intent")
        except ProviderNotConfigured:
            try:
                provider = await get_provider(session, "agent")
            except ProviderNotConfigured:
                log.warning("no intent/agent model configured; skipping classification")
                return None
        slots_desc = (
            "\n".join(f"- {s['name']}: {s.get('description', '')}" for s in wf.required_slots or [])
            or "(none)"
        )
        agent = Agent(build_model(provider), output_type=IntentVerdict)
        try:
            result = await agent.run(
                CLASSIFY_PROMPT.format(
                    trigger_prompt=wf.trigger_prompt,
                    slots_desc=slots_desc,
                    current_slots=render_slots(tstate.slots or {}),
                    window=rendered,
                )
            )
        except Exception:  # noqa: BLE001 — classifier failures must not stop the sweep
            log.exception("intent classification failed for workflow %s", wf.id)
            return None
        return result.output
