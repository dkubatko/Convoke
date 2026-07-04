"""Episode row helpers: load/open/close/touch and duplicate lookup.

Pure state decisions live in intent/state.py; this module is the thin layer
that applies them to IntentEpisode rows. No commits here — the caller owns
the transaction so an evaluation's episode mutations, cursor advance, and
optional PendingFire land atomically.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IntentEpisode, Workflow
from app.models.workflows import OPEN_EPISODE_STATUSES, PRE_FIRE_EPISODE_STATUSES


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def load_open_episodes(
    session: AsyncSession, workflow_id: int, chat_id: int, thread_key: int
) -> list[IntentEpisode]:
    return list(
        (
            await session.execute(
                select(IntentEpisode)
                .where(
                    IntentEpisode.workflow_id == workflow_id,
                    IntentEpisode.chat_id == chat_id,
                    IntentEpisode.thread_key == thread_key,
                    IntentEpisode.status.in_(OPEN_EPISODE_STATUSES),
                )
                .order_by(IntentEpisode.opened_at, IntentEpisode.id)
            )
        )
        .scalars()
        .all()
    )


def open_episode(
    session: AsyncSession,
    workflow_id: int,
    chat_id: int,
    thread_key: int,
    *,
    status: str,
    anchor_tg_message_id: int,
    summary: str,
    confidence: float,
    now: datetime,
) -> IntentEpisode:
    episode = IntentEpisode(
        workflow_id=workflow_id,
        chat_id=chat_id,
        thread_key=thread_key,
        status=status,
        slots={},
        summary=summary or None,
        anchor_tg_message_id=anchor_tg_message_id,
        confidence=confidence,
        opened_at=now,
        last_activity_at=now,
    )
    session.add(episode)
    return episode


def close_episode(episode: IntentEpisode, reason: str, now: datetime) -> None:
    episode.status = "closed"
    episode.close_reason = reason
    episode.closed_at = now


def touch(
    episode: IntentEpisode,
    now: datetime,
    *,
    summary: str | None = None,
    confidence: float | None = None,
) -> None:
    """Record attributed activity — the only thing that resets the idle clock."""
    episode.last_activity_at = now
    episode.unrelated_streak = 0
    if summary:
        episode.summary = summary
    if confidence is not None:
        episode.confidence = confidence


def pre_fire_episodes(episodes: list[IntentEpisode]) -> list[IntentEpisode]:
    return [e for e in episodes if e.status in PRE_FIRE_EPISODE_STATUSES]


def make_room(episodes: list[IntentEpisode], cap: int, now: datetime) -> bool:
    """Make room for one more pre-fire episode under the cap.

    Protection is earned by SUBSTANCE: a candidate with no gathered slots is
    evictable (oldest first) — a vague topic must never squat the cap against
    a concrete one. Slot-bearing candidates and parked (converged) episodes
    are immovable; when the cap is full of those, the new instance is not
    tracked (cap=1 keeps attribution easy until the small model is validated;
    see outstanding-issues)."""
    active = pre_fire_episodes(episodes)
    while len(active) >= cap:
        evictable = [e for e in active if e.status == "candidate" and not e.slots]
        if not evictable:
            return False
        close_episode(evictable[0], "superseded", now)
        active.remove(evictable[0])
    return True


async def revert_fired(session: AsyncSession, episode_id: int | None, now: datetime) -> None:
    """A fire that didn't happen (cancelled, errored, confirm timed out): the
    topic is still live — back to `candidate` so it can re-converge. fired_at
    is cleared so the aborted fire doesn't start a cooldown."""
    if episode_id is None:
        return
    episode = await session.get(IntentEpisode, episode_id)
    if episode is None or episode.status != "fired":
        return
    episode.status = "candidate"
    episode.fired_at = None
    episode.parked_at_tg_message_id = None
    episode.last_activity_at = now


async def finish_run_episode(
    session: AsyncSession, run_id: int, response_text: str | None, now: datetime
) -> None:
    """The feedback loop: when a workflow AgentRun ends, the linked episode
    becomes `satisfied` carrying WHAT WAS DONE (shown to the classifier so
    continuations of a handled topic are suppressed). A failed run reverts to
    `candidate` instead — the topic can re-converge on later activity."""
    episode = (
        await session.execute(select(IntentEpisode).where(IntentEpisode.agent_run_id == run_id))
    ).scalar_one_or_none()
    if episode is None or episode.status != "fired":
        return
    if response_text is None:
        episode.status = "candidate"
        episode.fired_at = None
    else:
        episode.status = "satisfied"
        episode.execution_summary = response_text[:500]
        episode.unrelated_streak = 0
    episode.last_activity_at = now


async def recent_duplicate(
    session: AsyncSession,
    workflow: Workflow,
    chat_id: int,
    slot_fingerprint: str,
    now: datetime,
    *,
    exclude_id: int | None = None,
) -> IntentEpisode | None:
    """An episode (chat-wide, any thread) that already converged on the same
    exact slot values within the dedup window."""
    window = timedelta(hours=workflow.dedup_window_hours or 24)
    rows = (
        (
            await session.execute(
                select(IntentEpisode).where(
                    IntentEpisode.workflow_id == workflow.id,
                    IntentEpisode.chat_id == chat_id,
                    IntentEpisode.fingerprint == slot_fingerprint,
                )
            )
        )
        .scalars()
        .all()
    )
    for ep in rows:
        if ep.id == exclude_id:
            continue
        if ep.status not in ("converged", "fired", "satisfied") and not (
            ep.status == "closed" and ep.close_reason == "done"
        ):
            continue
        anchor = as_utc(ep.fired_at) or as_utc(ep.last_activity_at)
        if anchor is not None and now - anchor <= window:
            return ep
    return None
