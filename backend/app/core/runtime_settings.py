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
    group: str = ""  # topic header the UI lists this setting under
    # When set, the UI renders a labelled N-stop control (one label per integer
    # from minimum..maximum) instead of a numeric slider — the operator picks a
    # named position and never sees the underlying value.
    step_labels: tuple[str, ...] | None = None


TUNABLES: list[Tunable] = [
    # --- per-chat: how a chat's conversation is windowed ---
    Tunable(
        "intent_lull_seconds", "Quiet gap before a check",
        "How long the chat must be quiet before the detector evaluates the latest burst. "
        "Lower reacts sooner; higher waits for the conversation to settle.",
        "seconds", 1, 3600, "chat", "chat", group="Windowing",
    ),
    Tunable(
        "intent_window_max_messages", "Messages that force a check",
        "The detector also evaluates once this many new messages pile up, without waiting "
        "for a quiet gap.",
        "messages", 2, 500, "chat", "chat", group="Windowing",
    ),
    # --- global, edited on Models: how the classifier model is called ---
    Tunable(
        "intent_min_llm_interval_seconds", "Minimum time between checks",
        "Circuit breaker: the classifier runs at most once per workflow per thread per this "
        "interval. The quiet gap is the real pacing — this just stops a runaway loop. Failed "
        "calls don't count; they retry at once.",
        "seconds", 0, 3600, "global", "models", group="Classifier",
    ),
    Tunable(
        "intent_classifier_concurrency", "Parallel classifier calls",
        "Classifier calls allowed to run at once across all chats. Higher clears busy sweeps "
        "faster; keep modest for local models.",
        "calls", 1, 16, "global", "models", group="Classifier",
    ),
    Tunable(
        "intent_context_messages", "Lead-up messages for the classifier",
        "Earlier messages shown to the classifier as context.",
        "messages", 0, 50, "global", "models", group="Classifier",
    ),
    # --- global, edited on Workflows: detector + firing behaviour ---
    Tunable(
        "intent_sweep_interval_seconds", "Detector loop interval",
        "How often the detector wakes to find chats ready to evaluate. One global loop serves "
        "every chat — the floor on how fast anything happens. Per-chat pacing is the quiet gap.",
        "seconds", 1, 300, "global", "workflows", group="Pacing & calibration",
    ),
    Tunable(
        "intent_example_count", "Example phrases per workflow",
        "Sample messages generated from each workflow's trigger to calibrate the prefilter. "
        "More = a steadier threshold, slower to generate. Applies on the next save.",
        "phrases", 6, 60, "global", "workflows", group="Pacing & calibration",
    ),
    Tunable(
        "intent_prefilter_permissiveness", "Prefilter permissiveness",
        "How readily the prefilter lets a conversation reach the classifier. Stricter filters "
        "harder (less noise, but on-topic messages are likelier missed); permissive lets "
        "borderline through (nothing lost, more classifier calls on chatter). Re-tunes every "
        "workflow instantly.",
        "", 1, 5, "global", "workflows", group="Pacing & calibration",
        step_labels=("Strictest", "Strict", "Balanced", "Permissive", "Most permissive"),
    ),
    Tunable(
        "intent_candidate_ttl_minutes", "Vague-topic lifetime",
        "A followed topic with no details yet is dropped after this long without on-topic "
        "activity. Once a detail lands, the longer idle limit below takes over.",
        "minutes", 1, 720, "global", "workflows", group="Topic tracking",
    ),
    Tunable(
        "intent_candidate_unrelated_k", "Off-topic checks to drop a vague topic",
        "A detail-less topic is also dropped after this many evaluations in a row judged "
        "unrelated — cheaper than waiting out its lifetime.",
        "checks", 1, 10, "global", "workflows", group="Topic tracking",
    ),
    Tunable(
        "intent_tracking_idle_hours", "Detailed-topic idle limit",
        "A topic with concrete details is closed after this long with no on-topic activity "
        "(off-topic chatter doesn't keep it alive).",
        "hours", 1, 168, "global", "workflows", group="Topic tracking",
    ),
    Tunable(
        "intent_decay_grace_hours", "Detail-decay grace period",
        "Each gathered detail (date, place, …) holds full confidence for this long after it "
        "was last stated, then begins to fade.",
        "hours", 0, 48, "global", "workflows", group="Topic tracking",
    ),
    Tunable(
        "intent_decay_per_hour_pct", "Detail decay per hour",
        "After the grace period, each detail keeps this % of its confidence per hour "
        "(85 → ~44% in 5h). Too-faded details stop counting toward firing; a re-mention "
        "restores them.",
        "percent", 50, 99, "global", "workflows", group="Topic tracking",
    ),
    Tunable(
        "intent_episode_max_age_days", "Topic hard age cap",
        "No topic is followed longer than this, whatever the activity — a backstop against a "
        "negotiation that never ends.",
        "days", 1, 30, "global", "workflows", group="Topic tracking",
    ),
    Tunable(
        "intent_max_open_episodes", "Topics followed at once",
        "In-progress topics one workflow tracks per thread. Keep at 1 until topic-attribution "
        "is proven; higher tracks interleaved conversations in parallel.",
        "topics", 1, 5, "global", "workflows", group="Topic tracking",
    ),
    Tunable(
        "intent_min_slot_confidence_pct", "Detail capture bar",
        "How sure the classifier must be before a heard detail is recorded at all. Below "
        "this, the mention is ignored.",
        "percent", 1, 100, "global", "workflows", group="Firing",
    ),
    Tunable(
        "intent_min_fire_confidence_pct", "Detail confidence to act",
        "Every required detail must be at least this confident for the workflow to fire. "
        "A detail between the capture bar and this shows amber — probable — and the "
        "classifier keeps trying to confirm it from the conversation. Keep at or above "
        "the capture bar.",
        "percent", 1, 100, "global", "workflows", group="Firing",
    ),
    Tunable(
        "confirm_timeout_minutes", "Confirmation timeout",
        "How long a confirmation prompt waits for an answer before it's cancelled.",
        "minutes", 1, 1440, "global", "workflows", group="Firing",
    ),
    Tunable(
        "memory_ignore_bot_messages", "Ignore bot messages in embeddings",
        "Applies to the connected bot and any member marked Bot on the chat's Members "
        "tab. When on (default), search ranking reads only human messages — bot replies "
        "tend to outrank the facts they summarize. Bot lines stay visible in results and "
        "exact-word search either way. Rebuild index (Role assignment tab) re-applies "
        "this to existing history.",
        "", 0, 1, "global", "models", group="Memory",
    ),
    Tunable(
        "chunk_target_tokens", "Memory chunk size",
        "How much conversation one searchable memory chunk holds, in embedding-model tokens. "
        "Smaller finds specific facts more precisely; larger keeps more surrounding context "
        "per hit. Always capped at the memory model's input window. Applies to newly closed "
        "chunks — rebuild the memory index (Models page) to re-cut existing history.",
        "tokens", 64, 1024, "global", "models", group="Memory",
    ),
    Tunable(
        "media_describe_concurrency", "Parallel media descriptions",
        "Photos/voice notes/videos described at once. Higher drains a backlog faster; keep "
        "modest for local vision/whisper models.",
        "workers", 1, 16, "global", "models", group="Media",
    ),
    Tunable(
        "intent_media_grace_seconds", "Media description grace",
        "If a window holds media still being described, the detector waits up to this long for "
        "it — so intent is judged on the media, not a placeholder.",
        "seconds", 0, 900, "global", "workflows", group="Firing",
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
