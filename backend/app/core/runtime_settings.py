"""Operator-tunable settings, overlaid on the env-based `Settings` defaults.

Each knob is either **global** (one value system-wide, edited on the page it
belongs to — Models for classifier cost, Workflows for detector behaviour) or
**per-chat** (each chat can override it from its Settings tab, falling back to
the global default). Overrides are persisted and merged over the defaults each
sweep, so changes take effect without a restart.
"""
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import ChatSetting, RuntimeSetting


@dataclass(frozen=True)
class Tunable:
    key: str  # must match a Settings field name
    label: str
    description: str
    unit: str
    minimum: int
    maximum: int
    scope: str  # "global" | "chat"
    page: str  # where it's edited: "models" | "workflows" | "chat"


TUNABLES: list[Tunable] = [
    # --- per-chat: how a chat's conversation is windowed ---
    Tunable(
        "intent_lull_seconds", "Quiet gap before a check",
        "How long this chat must be silent before the detector evaluates the latest burst. "
        "Lower reacts sooner after someone stops typing; higher waits for a more settled "
        "conversation.",
        "seconds", 1, 3600, "chat", "chat",
    ),
    Tunable(
        "intent_window_max_messages", "Messages that force a check",
        "The detector also evaluates once this many new messages pile up in this chat, "
        "without waiting for a quiet gap.",
        "messages", 2, 500, "chat", "chat",
    ),
    # --- global, edited on Models: how the classifier model is called ---
    Tunable(
        "intent_min_llm_interval_seconds", "Minimum time between checks",
        "A circuit breaker: the classifier model runs at most once per workflow per thread "
        "per this interval. The quiet-gap window is the real pacing — this only stops a "
        "pathological loop from hammering the model. (A failed call doesn't count — it "
        "retries right away.)",
        "seconds", 0, 3600, "global", "models",
    ),
    Tunable(
        "intent_classifier_concurrency", "Parallel classifier calls",
        "How many classifier calls may run at the same time across all chats and workflows. "
        "Higher clears busy sweeps faster; keep modest for local models.",
        "calls", 1, 16, "global", "models",
    ),
    Tunable(
        "intent_context_messages", "Lead-up messages for the classifier",
        "How many earlier messages are shown to the classifier model as context.",
        "messages", 0, 50, "global", "models",
    ),
    # --- global, edited on Workflows: detector + firing behaviour ---
    Tunable(
        "intent_sweep_interval_seconds", "Detector loop interval",
        "How often the detector wakes to look for chats ready to evaluate. One global loop "
        "handles every chat, so this is system-wide — the floor on how quickly anything can "
        "happen (per-chat responsiveness is the quiet gap in each chat's Settings).",
        "seconds", 1, 300, "global", "workflows",
    ),
    Tunable(
        "intent_example_count", "Example phrases per workflow",
        "How many sample messages the model generates from each workflow's trigger to calibrate "
        "the prefilter (more = a steadier threshold, slower to generate). Applies on the next "
        "save/edit of a workflow.",
        "phrases", 6, 60, "global", "workflows",
    ),
    Tunable(
        "intent_candidate_ttl_minutes", "Vague-topic lifetime",
        "A followed topic with no concrete details gathered yet is dropped after this much "
        "time without on-topic activity. Once a detail lands, the topic switches to the "
        "longer idle limit below.",
        "minutes", 1, 720, "global", "workflows",
    ),
    Tunable(
        "intent_candidate_unrelated_k", "Off-topic checks to drop a vague topic",
        "A topic with no details gathered is also dropped after this many consecutive "
        "evaluations judged unrelated — cheaper than waiting out its lifetime.",
        "checks", 1, 10, "global", "workflows",
    ),
    Tunable(
        "intent_tracking_idle_hours", "Detailed-topic idle limit",
        "A topic that has gathered concrete details is closed after this much time with no "
        "on-topic activity (off-topic chatter doesn't keep it alive).",
        "hours", 1, 168, "global", "workflows",
    ),
    Tunable(
        "intent_decay_grace_hours", "Detail-decay grace period",
        "Each gathered detail (date, place, …) keeps full confidence for this long after it "
        "was last stated in the chat; only then does it start to fade.",
        "hours", 0, 48, "global", "workflows",
    ),
    Tunable(
        "intent_decay_per_hour_pct", "Detail decay per hour",
        "After the grace period, each gathered detail keeps this percentage of its confidence "
        "per hour (85 = fades to ~44% in 5 hours). Details too faded no longer count toward "
        "firing; a re-mention restores full strength.",
        "%", 50, 99, "global", "workflows",
    ),
    Tunable(
        "intent_episode_max_age_days", "Topic hard age cap",
        "No topic is followed longer than this, regardless of activity — a backstop against "
        "a negotiation that never concludes.",
        "days", 1, 30, "global", "workflows",
    ),
    Tunable(
        "intent_max_open_episodes", "Topics followed at once",
        "How many separate in-progress topics one workflow tracks per thread. Keep at 1 until "
        "the classifier's topic-attribution quality is validated; raising it lets interleaved "
        "conversations be tracked in parallel.",
        "topics", 1, 5, "global", "workflows",
    ),
    Tunable(
        "confirm_timeout_minutes", "Confirmation timeout",
        "How long an in-chat confirmation prompt waits for an answer before it's cancelled.",
        "minutes", 1, 1440, "global", "workflows",
    ),
]

_BY_KEY = {t.key: t for t in TUNABLES}
CHAT_KEYS = [t.key for t in TUNABLES if t.scope == "chat"]


def default_for(key: str) -> int:
    return int(getattr(get_settings(), key))


def _check(key: str, value: int, scope: str | None = None) -> Tunable:
    t = _BY_KEY.get(key)
    if t is None:
        raise ValueError(f"unknown setting {key}")
    if scope is not None and t.scope != scope:
        raise ValueError(f"{key} is not a {scope} setting")
    if not (t.minimum <= value <= t.maximum):
        raise ValueError(f"{key} must be between {t.minimum} and {t.maximum}")
    return t


# ---------- global ----------

async def load_overrides(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(RuntimeSetting))).scalars().all()
    return {r.key: r.value for r in rows if r.key in _BY_KEY}


async def effective_settings(session: AsyncSession, base: Settings | None = None) -> Settings:
    """Base defaults with global overrides applied — safe on the hot path."""
    base = base or get_settings()
    overrides = await load_overrides(session)
    return base.model_copy(update=overrides) if overrides else base


async def set_override(session: AsyncSession, key: str, value: int) -> None:
    _check(key, value, scope="global")
    if value == default_for(key):  # storing the default just clears the override
        await session.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))
        return
    existing = await session.get(RuntimeSetting, key)
    if existing is None:
        session.add(RuntimeSetting(key=key, value=value))
    else:
        existing.value = value


# ---------- per-chat ----------

async def load_chat_overrides(session: AsyncSession, chat_id: int) -> dict[str, int]:
    rows = (
        await session.execute(select(ChatSetting).where(ChatSetting.chat_id == chat_id))
    ).scalars().all()
    return {r.key: r.value for r in rows if r.key in _BY_KEY}


async def set_chat_override(session: AsyncSession, chat_id: int, key: str, value: int) -> None:
    _check(key, value, scope="chat")
    # A chat override "of the default" is meaningful only against the *global*
    # effective value; we keep it simple and clear the row when it equals the
    # base default, so an unmodified chat inherits future default changes.
    if value == default_for(key):
        await session.execute(
            delete(ChatSetting).where(ChatSetting.chat_id == chat_id, ChatSetting.key == key)
        )
        return
    existing = await session.get(ChatSetting, (chat_id, key))
    if existing is None:
        session.add(ChatSetting(chat_id=chat_id, key=key, value=value))
    else:
        existing.value = value
