"""Intent sweeper: the episode-centric trigger pipeline.

Two phases per sweep:

Planning (free, no LLM) — enumerate eligible chats, close expired episodes,
window each (workflow × thread)'s unevaluated messages on a lull/burst, and
gate: a key with no ACTIVE episode (candidate/tracking/converged/fired) must
pass the embedding prefilter for the window to matter; an active episode is
sticky — every closed window goes to the classifier. Satisfied episodes are
prefilter-gated dedup memory: near-free to keep open, rendered to the
classifier whenever the gate passes. Parked (`converged`) episodes whose
cooldown lifted become recheck jobs.

Dispatch (cheap LLM, parallel) — jobs run concurrently under a global
semaphore, each in its own session/transaction. The classifier runs in
detect mode (no episodes: unrelated | ambiguous | active) or attribution
mode (episodes rendered with their summaries and, post-fire, what the agent
already did: unrelated | continues_episode | new_instance). Verdicts drive
the episode state machine; an evaluation's episode mutations, cursor
advance, and optional PendingFire commit atomically.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from pydantic_ai import Agent
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import ProviderNotConfigured, build_model, evict_model, get_provider
from app.core.config import get_settings
from app.core.runtime_settings import effective_settings, load_chat_overrides
from app.intent.episodes import (
    as_utc,
    close_episode,
    load_open_episodes,
    make_room,
    open_episode,
    pre_fire_episodes,
    recent_duplicate,
    touch,
)
from app.intent.examples import dot, load_positive_vectors
from app.intent.prompts import (
    build_attribution_prompt,
    build_detect_prompt,
    build_recheck_prompt,
)
from app.intent.schemas import AttributionVerdict, DetectVerdict, RecheckVerdict
from app.media.render import attachment_annotation, message_body
from app.intent.state import (
    MIN_FIRE_CONFIDENCE,
    apply_slot_updates,
    effective_slots,
    fingerprint,
    is_converged,
    lifecycle_close_reason,
    normalize_slot_updates,
    render_slots,
)
from app.memory.chunker import resolve_reply_targets
from app.memory.embeddings import Embedder
from app.threads import unmonitored_threads
from app.models import (
    Chat,
    ChatThread,
    IntentCursor,
    IntentEpisode,
    Message,
    PendingFire,
    Workflow,
    WorkflowAssignment,
)

log = logging.getLogger("convoke.intent")

Key = tuple[int, int, int]  # (workflow_id, chat_id, thread_key)


def gate_texts(m: Message, target: Message | None) -> list[str]:
    """Candidate texts a message contributes to the prefilter — the message's
    gate score is the max over them.

    Media messages get their components scored SEPARATELY as well as combined:
    a verbose vision description concatenated with a short caption dilutes the
    caption's intent signal in a single embedding, so max-over-components lets
    the strongest signal through.

    A plain text-only, reply-free message yields exactly [m.text] — identical
    to the pre-media behavior."""
    candidates: list[str] = []
    if m.text:
        candidates.append(m.text)
    if m.attachment is not None:
        candidates.append(attachment_annotation(m.attachment))
        if m.text:
            candidates.append(message_body(m))  # combined, for good measure
    # Replies inherit their target's topicality ("cool!" replying to "let's
    # hike in Sunnyvale" must score like the proposal) — as an ADDITIONAL
    # candidate, so a strong bare caption can't be dragged down by it either.
    if target is not None and message_body(target):
        candidates.append(f"{message_body(target)}\n{message_body(m)}")
    seen: set[str] = set()
    unique = [c for c in candidates if c and not (c in seen or seen.add(c))]
    return unique or [message_body(m)]


@dataclass
class EvalJob:
    workflow: Workflow
    chat_id: int
    thread_key: int
    window: list[Message]  # non-self burst being consumed
    transcript_window: list[Message]  # same span, bot messages interleaved
    context: list[Message]  # lead-up before the window, bot messages included
    score: float | None  # prefilter score, when it ran
    # Resolved reply targets for the window: the transcript renders a short
    # pointer when the target is visible, or the full quoted original when it
    # is not — a reply after a long pause carries its context, unduplicated.
    quoted: dict[int, Message] = field(default_factory=dict)

    @property
    def key(self) -> Key:
        return (self.workflow.id, self.chat_id, self.thread_key)


@dataclass
class RecheckJob:
    workflow: Workflow
    chat_id: int
    thread_key: int
    episode_id: int

    @property
    def key(self) -> Key:
        return (self.workflow.id, self.chat_id, self.thread_key)


@dataclass
class _ChatPlan:
    jobs: list = field(default_factory=list)


class IntentSweeper:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        # `_base` is the pristine env defaults; `settings` is refreshed each sweep
        # with operator overrides so tuning takes effect without a restart. Tests
        # override `_base` to inject config.
        self._base = get_settings()
        self.settings = self._base
        # Keys with an evaluation in flight — planning skips them so two sweeps
        # never evaluate the same (workflow, chat, thread) concurrently.
        self._in_flight: set[Key] = set()

    # ---------- sweep ----------

    async def sweep(self, now: datetime | None = None) -> int:
        """One pass over all eligible chats; returns evaluations performed."""
        now = now or datetime.now(timezone.utc)
        async with self.sessionmaker() as session:
            self.settings = await effective_settings(session, self._base)
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

        jobs: list[EvalJob | RecheckJob] = []
        for chat_id in chat_ids:
            try:
                jobs.extend(await self._plan_chat(chat_id, now))
            except Exception:  # noqa: BLE001 — one bad chat must not stop the sweep
                log.exception("intent planning failed for chat %s", chat_id)
        if not jobs:
            return 0

        semaphore = asyncio.Semaphore(max(1, self.settings.intent_classifier_concurrency))
        try:
            results = await asyncio.gather(
                *(self._run_job(job, semaphore, now) for job in jobs),
                return_exceptions=True,
            )
        finally:
            for job in jobs:
                self._in_flight.discard(job.key)
        evaluated = 0
        for job, result in zip(jobs, results):
            if isinstance(result, Exception):
                log.error("intent job failed for %s: %r", job.key, result)
            elif result:
                evaluated += 1
        return evaluated

    # ---------- planning (no LLM) ----------

    async def _plan_chat(self, chat_id: int, now: datetime) -> list[EvalJob | RecheckJob]:
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
                return []
            wf_by_id = {w.id: w for w in workflows}

            episodes_by_key = await self._close_expired_episodes(
                session, wf_by_id, chat_id, now
            )

            cursors = (
                (
                    await session.execute(
                        select(IntentCursor).where(
                            IntentCursor.workflow_id.in_(wf_by_id),
                            IntentCursor.chat_id == chat_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            cursor_by_key: dict[Key, IntentCursor] = {
                (c.workflow_id, c.chat_id, c.thread_key): c for c in cursors
            }

            # Load the tail of the chat past the least-advanced cursor. Bot
            # messages are fetched too — they render as [bot] context lines —
            # but never count as unevaluated work.
            min_cursor = min(
                (c.last_tg_message_id for c in cursors), default=0
            )
            rows = (
                (
                    await session.execute(
                        select(Message)
                        .where(Message.chat_id == chat_id, Message.tg_message_id > min_cursor)
                        .order_by(Message.tg_message_id.desc())
                        .limit(300)
                    )
                )
                .scalars()
                .all()
            )
            messages = list(reversed(rows))

            by_thread: dict[int, list[Message]] = {}
            for m in messages:
                by_thread.setdefault(m.thread_id or 0, []).append(m)

            chat_ov = await load_chat_overrides(session, chat_id)
            max_win = chat_ov.get(
                "intent_window_max_messages", self.settings.intent_window_max_messages
            )
            lull = chat_ov.get("intent_lull_seconds", self.settings.intent_lull_seconds)

            # Unmonitored threads are fully ignored — the detector never looks at
            # them (existing episodes there are left to age out on their own).
            unmonitored = await unmonitored_threads(session, chat_id)

            jobs: list[EvalJob | RecheckJob] = []
            for wf in workflows:
                thread_keys = set(by_thread) | {
                    key[2] for key in episodes_by_key if key[0] == wf.id
                }
                for thread_key in thread_keys:
                    if thread_key in unmonitored:
                        continue
                    if (wf.id, chat_id, thread_key) in self._in_flight:
                        continue
                    job = await self._plan_key(
                        session,
                        wf,
                        chat_id,
                        thread_key,
                        by_thread.get(thread_key, []),
                        cursor_by_key,
                        episodes_by_key.get((wf.id, chat_id, thread_key), []),
                        max_win,
                        lull,
                        now,
                    )
                    if job is not None:
                        jobs.append(job)
                        self._in_flight.add(job.key)
            await session.commit()
            return jobs

    async def _close_expired_episodes(
        self, session: AsyncSession, wf_by_id: dict[int, Workflow], chat_id: int, now: datetime
    ) -> dict[Key, list[IntentEpisode]]:
        """Time/streak lifecycle pass; returns surviving open episodes by key."""
        s = self.settings
        open_eps = (
            (
                await session.execute(
                    select(IntentEpisode).where(
                        IntentEpisode.workflow_id.in_(wf_by_id),
                        IntentEpisode.chat_id == chat_id,
                        IntentEpisode.status.in_(
                            ("candidate", "converged", "fired", "satisfied")
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_key: dict[Key, list[IntentEpisode]] = {}
        for ep in open_eps:
            wf = wf_by_id[ep.workflow_id]
            reason = lifecycle_close_reason(
                ep.status,
                as_utc(ep.opened_at),
                as_utc(ep.last_activity_at),
                ep.unrelated_streak,
                bool(ep.slots),
                now,
                candidate_ttl=timedelta(minutes=s.intent_candidate_ttl_minutes),
                candidate_unrelated_k=s.intent_candidate_unrelated_k,
                invested_idle=timedelta(hours=s.intent_tracking_idle_hours),
                max_age=timedelta(days=s.intent_episode_max_age_days),
                dedup_window=timedelta(hours=wf.dedup_window_hours or 24),
            )
            if reason is not None:
                close_episode(ep, reason, now)
                continue
            by_key.setdefault((ep.workflow_id, ep.chat_id, ep.thread_key), []).append(ep)
        for eps in by_key.values():
            eps.sort(key=lambda e: (as_utc(e.opened_at), e.id))
        return by_key

    async def _plan_key(
        self,
        session: AsyncSession,
        wf: Workflow,
        chat_id: int,
        thread_key: int,
        thread_msgs: list[Message],
        cursor_by_key: dict[Key, IntentCursor],
        episodes: list[IntentEpisode],
        max_win: int,
        lull: int,
        now: datetime,
    ) -> EvalJob | RecheckJob | None:
        key = (wf.id, chat_id, thread_key)
        cursor = cursor_by_key.get(key)
        if cursor is None:
            # First sight of this key: seed at the chat's current tail so a
            # newly assigned workflow never chews through historical backlog.
            # (A dedicated max() query — the planning fetch is bounded by the
            # other cursors and may not reach the true tail.) The assignment
            # API seeds authoritatively; this is the safety net.
            tail = await session.scalar(
                select(func.max(Message.tg_message_id)).where(Message.chat_id == chat_id)
            )
            cursor = IntentCursor(
                workflow_id=wf.id,
                chat_id=chat_id,
                thread_key=thread_key,
                last_tg_message_id=tail or 0,
            )
            session.add(cursor)
            cursor_by_key[key] = cursor
            return None

        # A parked episode whose cooldown lifted gets rechecked/fired even if
        # the chat has been silent since.
        parked = [e for e in episodes if e.status == "converged"]
        if parked and not self._cooldown_active(wf, episodes, now):
            return RecheckJob(
                workflow=wf, chat_id=chat_id, thread_key=thread_key, episode_id=parked[0].id
            )

        unevaluated = [
            m
            for m in thread_msgs
            if m.source != "self" and m.tg_message_id > cursor.last_tg_message_id
        ]
        if not unevaluated:
            return None
        last_at = as_utc(unevaluated[-1].sent_at)
        if len(unevaluated) < max_win and (now - last_at).total_seconds() < lull:
            return None  # window still open
        window = unevaluated[-max_win:]

        # Media in the window still being described: hold the window briefly
        # so the classifier judges the description, not a pending placeholder.
        # Past the grace it evaluates anyway — attribution/recheck refine later.
        grace = self.settings.intent_media_grace_seconds
        for m in window:
            att = m.attachment
            if (
                att is not None
                and att.status == "pending"
                and (now - as_utc(m.sent_at)).total_seconds() < grace
            ):
                return None

        # Replies inherit their target's topicality: resolve replied-to
        # messages once — combined with the reply for prefilter scoring
        # ("cool!" replying to "let's hike in Sunnyvale" must score like the
        # proposal), and quoted in the transcript when outside it.
        reply_targets = await resolve_reply_targets(session, chat_id, thread_msgs)

        # Stickiness (bypassing the prefilter) exists so an ACTIVE negotiation
        # never loses a weak-looking follow-up ("yes, 7 works"). A satisfied
        # episode is memory, not activity: the embedding gate resumes — weak
        # follow-ups to a handled topic are suppressed for free — and when
        # something on-intent does pass, the classifier still sees the handled
        # topic (attribution mode), with the skipped lines in its lead-up
        # context. Keeping a handled topic open costs ~nothing.
        sticky = any(e.status != "satisfied" for e in episodes)
        score: float | None = None
        if not sticky:
            # The prefilter decides whether the classifier looks at all.
            positives = await load_positive_vectors(session, wf.id)
            if positives:
                self._mark(cursor, now, "evaluating_prefilter")
                await session.commit()
                flat = [
                    text
                    for m in window
                    for text in gate_texts(
                        m, reply_targets.get(m.reply_to_tg_message_id or 0)
                    )
                ]
                vecs = await self.embedder.embed_passages(flat)
                score = max(dot(v, p) for v in vecs for p in positives)
                if score < (wf.threshold or 0.8):
                    cursor.last_tg_message_id = window[-1].tg_message_id
                    self._mark(cursor, now, "prefilter_skip", score=score)
                    return None

        # LLM circuit breaker: a *successful* call ran too recently. The
        # cursor is not advanced — the window re-evaluates next sweep.
        last_llm = as_utc(cursor.last_llm_at)
        if (
            last_llm is not None
            and (now - last_llm).total_seconds() < self.settings.intent_min_llm_interval_seconds
        ):
            self._mark(cursor, now, "throttled", score=score)
            return None

        transcript_window = [
            m
            for m in thread_msgs
            if window[0].tg_message_id <= m.tg_message_id <= window[-1].tg_message_id
        ]
        context = await self._prior_context(session, chat_id, thread_key, window[0])
        return EvalJob(
            workflow=wf,
            chat_id=chat_id,
            thread_key=thread_key,
            window=window,
            transcript_window=transcript_window,
            context=context,
            score=score,
            quoted=reply_targets,
        )

    def _cooldown_active(
        self, wf: Workflow, episodes: list[IntentEpisode], now: datetime
    ) -> bool:
        if not wf.cooldown_seconds:
            return False
        fired = [as_utc(e.fired_at) for e in episodes if e.fired_at is not None]
        if not fired:
            return False
        return now < max(fired) + timedelta(seconds=wf.cooldown_seconds)

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
                        Message.thread_id.is_(None)
                        if thread_key == 0
                        else Message.thread_id == thread_key,
                    )
                    .order_by(Message.tg_message_id.desc())
                    .limit(self.settings.intent_context_messages)
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))

    # ---------- dispatch (LLM, parallel) ----------

    async def _run_job(
        self, job: EvalJob | RecheckJob, semaphore: asyncio.Semaphore, now: datetime
    ) -> bool:
        async with semaphore:
            try:
                if isinstance(job, RecheckJob):
                    return await self._run_recheck(job, now)
                return await self._evaluate_window(job, now)
            finally:
                self._in_flight.discard(job.key)

    async def _evaluate_window(self, job: EvalJob, now: datetime) -> bool:
        wf = job.workflow
        async with self.sessionmaker() as session:
            cursor = await session.get(IntentCursor, (wf.id, job.chat_id, job.thread_key))
            if cursor is None:
                return False
            window = [m for m in job.window if m.tg_message_id > cursor.last_tg_message_id]
            if not window:
                return False
            episodes = await load_open_episodes(session, wf.id, job.chat_id, job.thread_key)

            # Publish the in-flight marker (committed now) so the UI shows the
            # check running during the model call.
            self._mark(cursor, now, "evaluating", score=job.score)
            await session.commit()

            if episodes:
                prompt = build_attribution_prompt(
                    wf, episodes, job.context, job.transcript_window, job.quoted
                )
                verdict = await self._model_call(session, prompt, AttributionVerdict)
            else:
                prompt = build_detect_prompt(wf, job.context, job.transcript_window, job.quoted)
                verdict = await self._model_call(session, prompt, DetectVerdict)
            if verdict is None:
                # A FAILED call must not consume the window or spend the
                # throttle budget — the next sweep retries promptly.
                self._mark(cursor, now, "classifier_error", score=job.score)
                await session.commit()
                return False

            # Small models invent slot names; remap/drop before anything
            # stores them (a phantom slot can never satisfy convergence).
            verdict.slot_updates = normalize_slot_updates(
                verdict.slot_updates, wf.required_slots or []
            )
            # The verdict log is the attribution-quality audit trail — the
            # thing to read when a topic didn't split/fold the way expected.
            log.info(
                "verdict wf=%s chat=%s thread=%s mode=%s relation=%s ref=%s conf=%.2f "
                "concluded=%s slots=%s summary=%r",
                wf.id,
                job.chat_id,
                job.thread_key,
                "attribution" if episodes else "detect",
                verdict.relation,
                getattr(verdict, "episode_ref", None),
                verdict.confidence,
                getattr(verdict, "topic_concluded", False),
                [u.name for u in verdict.slot_updates],
                (verdict.topic_summary or "")[:120],
            )
            cursor.last_llm_at = now
            if episodes:
                stage = await self._apply_attribution(session, job, episodes, verdict, now)
            else:
                stage = await self._apply_detect(session, job, verdict, now)
            cursor.last_tg_message_id = window[-1].tg_message_id
            self._mark(cursor, now, stage, score=job.score, confidence=verdict.confidence)
            await session.commit()
            return True

    async def _apply_detect(
        self, session: AsyncSession, job: EvalJob, verdict: DetectVerdict, now: datetime
    ) -> str:
        if verdict.relation == "unrelated":
            return "no_match"
        # One pre-fire state: everything opens as `candidate`; its leash and
        # cap protection derive from gathered slots, not from a status label.
        # The ambiguous/clear distinction survives only as a fire gate: an
        # "ambiguous" maybe must never fire a no-slot workflow on the spot.
        episode = open_episode(
            session,
            job.workflow.id,
            job.chat_id,
            job.thread_key,
            status="candidate",
            anchor_tg_message_id=job.window[0].tg_message_id,
            summary=verdict.topic_summary,
            confidence=verdict.confidence,
            now=now,
        )
        episode.slots = apply_slot_updates(
            {}, verdict.slot_updates, now, job.window[-1].tg_message_id
        )
        if verdict.relation == "ambiguous":
            return "candidate"
        stage = await self._maybe_fire(session, job, episode, verdict.confidence, now)
        return stage if episode.slots or stage != "accumulating" else "candidate"

    async def _apply_attribution(
        self,
        session: AsyncSession,
        job: EvalJob,
        episodes: list[IntentEpisode],
        verdict: AttributionVerdict,
        now: datetime,
    ) -> str:
        if verdict.relation == "unrelated":
            for ep in episodes:
                if ep.status == "candidate" and not ep.slots:
                    ep.unrelated_streak += 1
                    if ep.unrelated_streak >= self.settings.intent_candidate_unrelated_k:
                        close_episode(ep, "expired", now)
            return "no_match"

        if verdict.relation == "new_instance":
            if not make_room(episodes, self.settings.intent_max_open_episodes, now):
                # Cap full of invested (slot-bearing/parked) topics: the new
                # occurrence is not tracked (see outstanding-issues).
                return "cap_full"
            episode = open_episode(
                session,
                job.workflow.id,
                job.chat_id,
                job.thread_key,
                status="candidate",
                anchor_tg_message_id=job.window[0].tg_message_id,
                summary=verdict.topic_summary,
                confidence=verdict.confidence,
                now=now,
            )
            episode.slots = apply_slot_updates(
                {}, verdict.slot_updates, now, job.window[-1].tg_message_id
            )
            stage = await self._maybe_fire(session, job, episode, verdict.confidence, now)
            return stage if episode.slots or stage != "accumulating" else "candidate"

        # continues_episode
        episode = self._resolve_ref(episodes, verdict.episode_ref)
        if episode.status in ("fired", "satisfied"):
            # Continuation of a handled topic — the double-fire killer. Keep
            # the suppression window warm, but NEVER rewrite the topic's
            # summary: a single misattributed window would re-identify the
            # handled topic and make every later misattribution
            # self-confirming (the topic swallows its neighbors). A handled
            # topic's identity is frozen at what was handled.
            touch(episode, now)
            return "suppressed"
        if verdict.topic_concluded:
            close_episode(episode, "abandoned", now)
            return "concluded"
        episode.slots = apply_slot_updates(
            dict(episode.slots or {}), verdict.slot_updates, now, job.window[-1].tg_message_id
        )
        touch(episode, now, summary=verdict.topic_summary or None, confidence=verdict.confidence)
        if episode.status == "converged":
            return "parked"  # still waiting out the cooldown; slots refreshed
        stage = await self._maybe_fire(session, job, episode, verdict.confidence, now)
        return stage if episode.slots or stage != "accumulating" else "candidate"

    @staticmethod
    def _resolve_ref(episodes: list[IntentEpisode], ref: int | None) -> IntentEpisode:
        """episode_ref is a 1-based index into the rendered list; small models
        sometimes omit or botch it — fall back to the most recently active."""
        if ref is not None and 1 <= ref <= len(episodes):
            return episodes[ref - 1]
        return max(episodes, key=lambda e: (as_utc(e.last_activity_at), e.id))

    async def _maybe_fire(
        self,
        session: AsyncSession,
        job: EvalJob,
        episode: IntentEpisode,
        confidence: float,
        now: datetime,
    ) -> str:
        wf = job.workflow
        if confidence < MIN_FIRE_CONFIDENCE:
            return "accumulating"
        effective = effective_slots(
            dict(episode.slots or {}),
            now,
            timedelta(hours=self.settings.intent_decay_grace_hours),
            self.settings.intent_decay_per_hour_pct / 100,
        )
        if not is_converged(wf.required_slots or [], effective):
            return "accumulating"

        # Fingerprint dedup only means something over actual values — for a
        # no-slot workflow every episode would collide on the empty hash, and
        # its dedup is the classifier's continuation verdict alone.
        if episode.slots:
            episode.fingerprint = fingerprint(episode.slots)
            duplicate = await recent_duplicate(
                session, wf, job.chat_id, episode.fingerprint, now, exclude_id=episode.id
            )
            if duplicate is not None:
                close_episode(episode, "duplicate", now)
                return "duplicate"

        others = await load_open_episodes(session, wf.id, job.chat_id, job.thread_key)
        if self._cooldown_active(wf, others, now):
            episode.status = "converged"
            episode.parked_at_tg_message_id = job.window[-1].tg_message_id
            return "parked"
        self._fire(session, wf, episode, now)
        return "fired"

    def _fire(
        self, session: AsyncSession, wf: Workflow, episode: IntentEpisode, now: datetime
    ) -> None:
        session.add(
            PendingFire(
                workflow_id=wf.id,
                chat_id=episode.chat_id,
                thread_key=episode.thread_key,
                episode_id=episode.id,
                slots=dict(episode.slots or {}),
                status="pending",
            )
        )
        episode.status = "fired"
        episode.fired_at = now
        episode.parked_at_tg_message_id = None
        touch(episode, now)
        log.info(
            "workflow %s converged in chat %s: %s",
            wf.id,
            episode.chat_id,
            render_slots(episode.slots or {}),
        )

    # ---------- recheck (park-and-recheck cooldown exit) ----------

    async def _run_recheck(self, job: RecheckJob, now: datetime) -> bool:
        wf = job.workflow
        async with self.sessionmaker() as session:
            episode = await session.get(IntentEpisode, job.episode_id)
            if episode is None or episode.status != "converged":
                return False
            cursor = await session.get(IntentCursor, (wf.id, job.chat_id, job.thread_key))
            since = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.chat_id == job.chat_id,
                            Message.tg_message_id > (episode.parked_at_tg_message_id or 0),
                        )
                        .order_by(Message.tg_message_id.desc())
                        .limit(30)
                    )
                )
                .scalars()
                .all()
            )
            since = list(reversed(since))
            if since:
                # Something was said while parked — one cheap check that the
                # group still wants this before acting late.
                if cursor is not None:
                    self._mark(cursor, now, "rechecking")
                    await session.commit()
                verdict = await self._model_call(
                    session, build_recheck_prompt(wf, episode, since), RecheckVerdict
                )
                if verdict is None:
                    return False  # stay parked; retry next sweep
                log.info(
                    "verdict wf=%s chat=%s thread=%s mode=recheck still_wanted=%s conf=%.2f reason=%r",
                    wf.id, job.chat_id, job.thread_key,
                    verdict.still_wanted, verdict.confidence, verdict.reason[:120],
                )
                if not verdict.still_wanted:
                    close_episode(episode, "stale", now)
                    if cursor is not None:
                        self._mark(cursor, now, "stale")
                    await session.commit()
                    return True
                if episode.slots:  # slots may have refreshed while parked
                    episode.fingerprint = fingerprint(episode.slots)
            self._fire(session, wf, episode, now)
            if cursor is not None:
                self._mark(cursor, now, "fired")
            await session.commit()
            return True

    # ---------- shared ----------

    @staticmethod
    def _mark(
        cursor: IntentCursor,
        now: datetime,
        stage: str,
        score: float | None = None,
        confidence: float | None = None,
    ) -> None:
        cursor.last_evaluated_at = now
        cursor.last_stage = stage
        cursor.last_score = score
        cursor.last_confidence = confidence

    async def _model_call(
        self, session: AsyncSession, prompt: str, output_type: type[BaseModel]
    ) -> BaseModel | None:
        """The single seam to the cheap model — tests monkeypatch this."""
        try:
            provider = await get_provider(session, "intent")
        except ProviderNotConfigured:
            try:
                provider = await get_provider(session, "agent")
            except ProviderNotConfigured:
                log.warning("no intent/agent model configured; skipping classification")
                return None
        # Small local models often need a couple of tries to produce valid
        # structured output; pydantic-ai re-prompts with the validation error.
        agent = Agent(build_model(provider), output_type=output_type, retries=3)
        try:
            result = await agent.run(prompt)
        except Exception:  # noqa: BLE001 — classifier failures must not stop the sweep
            log.exception("intent classification failed")
            evict_model(provider)  # a poisoned pooled client must not survive the retry
            return None
        return result.output
