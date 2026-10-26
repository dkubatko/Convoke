"""Agent tasks: one-off and recurring tasks the agent schedules in a chat.

Validation lives here so the agent tool and the API share one rulebook; the
ScheduleLoop calls fire_due_tasks on its tick. Limits exist to stop a chat
(or a prompt-injected agent) from turning the bot into a spam cannon:
hourly at most, a year ahead at most, a bounded number per chat, and only
member-invoked runs may create tasks (a task can't breed tasks).
"""

from datetime import datetime, timedelta, timezone

from croniter import croniter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_tzinfo
from app.models import AgentRun, AgentTask, Chat

MIN_LEAD = timedelta(minutes=1)
MAX_HORIZON = timedelta(days=365)
MIN_INTERVAL = timedelta(hours=1)
MAX_ACTIVE_PER_CHAT = 25
MAX_TASK_CHARS = 2000
# A fire later than this (worker down) is recorded as skipped, not run: a
# "lunch in 10 minutes" reminder hours late is worse than none.
MISSED_GRACE = timedelta(hours=1)
# Upcoming fires sampled to verify the minimum interval — enough to catch
# patterns whose short gap occurs once a month for two years.
INTERVAL_SAMPLES = 400


class ScheduleError(ValueError):
    """A schedule the rules reject; the message is shown to the agent/operator."""


def utc(dt: datetime | None) -> datetime | None:
    # SQLite (tests) hands back naive datetimes where Postgres returns aware.
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _aware(dt: datetime) -> datetime:
    # Naive datetimes mean the group's clock — the same reference timezone
    # transcripts and cron fields use.
    return dt.replace(tzinfo=get_tzinfo()) if dt.tzinfo is None else dt


def cron_next(cron: str, after: datetime) -> datetime:
    """Next fire strictly after `after`, cron fields in the reference
    timezone; returned in UTC."""
    local = croniter(cron, after.astimezone(get_tzinfo())).get_next(datetime)
    return local.astimezone(timezone.utc)


def check_cron(cron: str, now: datetime) -> datetime:
    """Validate a recurring schedule; returns its first fire (UTC)."""
    if not croniter.is_valid(cron) or len(cron.split()) != 5:
        raise ScheduleError(
            f"{cron!r} isn't a valid 5-field cron expression (minute hour day month weekday)."
        )
    it = croniter(cron, now.astimezone(get_tzinfo()))
    fires = [it.get_next(datetime).astimezone(timezone.utc) for _ in range(INTERVAL_SAMPLES)]
    first = fires[0]
    if first - now > MAX_HORIZON:
        raise ScheduleError("The first run must fall within a year from now.")
    gaps = [b - a for a, b in zip(fires, fires[1:]) if a - now <= MAX_HORIZON]
    if gaps and min(gaps) < MIN_INTERVAL:
        raise ScheduleError("Recurring tasks can run at most once an hour.")
    return first


def plan_schedule(
    now: datetime,
    at: datetime | None,
    repeat: str | None,
    ends: datetime | None,
) -> tuple[datetime | None, str | None, datetime | None, datetime]:
    """Validate a requested schedule → (run_at, cron, ends_at, next_fire_at)."""
    repeat = (repeat or "").strip() or None
    if (at is None) == (repeat is None):
        raise ScheduleError("Give exactly one of `at` (one-off) or `repeat` (recurring cron).")
    if at is not None:
        if ends is not None:
            raise ScheduleError("`ends` only applies to recurring tasks.")
        at = _aware(at).astimezone(timezone.utc)
        if at - now < MIN_LEAD:
            raise ScheduleError("A one-off task must be at least a minute in the future.")
        if at - now > MAX_HORIZON:
            raise ScheduleError("A task can be scheduled at most a year ahead.")
        return at, None, None, at
    first = check_cron(repeat, now)
    cap = now + MAX_HORIZON
    ends_at = cap if ends is None else _aware(ends).astimezone(timezone.utc)
    if ends_at > cap:
        raise ScheduleError("A recurring task can run for at most a year — pick an earlier end.")
    if ends_at < first:
        raise ScheduleError("That end date is before the first run.")
    return None, repeat, ends_at, first


async def active_count(session: AsyncSession, chat_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(AgentTask).where(
            AgentTask.chat_id == chat_id, AgentTask.status == "active"
        )
    ) or 0


def advance(task: AgentTask, now: datetime) -> None:
    """Move a task past a fire: a one-off is done; a recurring task gets its
    next fire, or ends when that would pass its end date."""
    task.last_fired_at = now
    if not task.cron:
        task.status = "done"
        task.next_fire_at = None
        return
    nxt = cron_next(task.cron, now)
    ends_at = utc(task.ends_at)
    if ends_at is not None and nxt > ends_at:
        task.status = "ended"
        task.next_fire_at = None
    else:
        task.next_fire_at = nxt


async def fire_due_tasks(session: AsyncSession, now: datetime) -> int:
    """Queue a run for every due task (caller commits). Late beyond the grace
    window → a skipped run is recorded instead, so the activity log shows it."""
    due = (
        await session.execute(
            select(AgentTask)
            .join(Chat, Chat.id == AgentTask.chat_id)
            .where(
                AgentTask.status == "active",
                AgentTask.enabled.is_(True),
                AgentTask.next_fire_at <= now,
                Chat.status == "authorized",
            )
        )
    ).scalars().all()
    fired = 0
    for task in due:
        due_at = utc(task.next_fire_at)
        run = AgentRun(
            chat_id=task.chat_id,
            trigger="task",
            task_id=task.id,
            thread_id=task.thread_id,
            request_text=task.task,
        )
        if now - due_at > MISSED_GRACE:
            run.status = "skipped"
            run.error = (
                f"missed: was due {due_at:%Y-%m-%d %H:%M} UTC while the worker was offline"
            )
            run.finished_at = now
        else:
            fired += 1
        session.add(run)
        advance(task, now)
    return fired
