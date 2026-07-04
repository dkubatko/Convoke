from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base
from app.models.memory import EmbeddingVariant

WORKFLOW_TYPES = ("scheduled", "intent")
EXAMPLES_STATUSES = ("pending", "ready", "fallback")
FIRE_STATUSES = ("pending", "confirm_wait", "confirmed", "done", "cancelled", "error")
LIVE_FIRE_STATUSES = ("pending", "confirm_wait", "confirmed")

# Episode lifecycle. One pre-fire state: `candidate` — its leash (idle
# lifetime, eviction protection) is derived from gathered SUBSTANCE (slots),
# not from a status label. `converged` is a parked candidate waiting out a
# rate limit. Active states keep the thread sticky (prefilter bypassed);
# satisfied is prefilter-gated dedup memory. Only pre-fire states count
# against the open-episode cap — a fired/satisfied episode must never block
# a genuinely new occurrence from opening.
EPISODE_STATUSES = ("candidate", "converged", "fired", "satisfied", "closed")
OPEN_EPISODE_STATUSES = ("candidate", "converged", "fired", "satisfied")
PRE_FIRE_EPISODE_STATUSES = ("candidate", "converged")
EPISODE_CLOSE_REASONS = ("expired", "abandoned", "duplicate", "stale", "done", "superseded")


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)  # scheduled | intent
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    action_prompt: Mapped[str] = mapped_column(Text)

    # scheduled
    cron: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # intent
    trigger_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"name": "date", "description": "the agreed date and time"}, …]
    required_slots: Mapped[list] = mapped_column(JSON, default=list)
    confirm: Mapped[bool] = mapped_column(Boolean, default=True)
    # Optional rate limit, independent of dedup: a minimum interval (seconds)
    # between fires. 0 = off (the default). A converged episode PARKS during
    # the cooldown and is rechecked when it lifts — never dropped.
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0)
    # How long a satisfied episode stays open as dedup memory: it suppresses
    # same-topic continuations that pass the prefilter, and its fingerprint
    # blocks exact-duplicate convergences. Prefilter-gated, so near-free.
    dedup_window_hours: Mapped[int] = mapped_column(Integer, default=12)
    # embedding-prefilter threshold, calibrated from generated examples
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    examples_status: Mapped[str] = mapped_column(Text, default="pending")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkflowExample(Base):
    """Synthetic example utterances anchoring the intent prefilter in
    utterance space (matching real chat text against the trigger PROMPT
    fails — prompts are meta-language)."""

    __tablename__ = "workflow_examples"
    __table_args__ = (Index("ix_workflow_examples_wf", "workflow_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)  # positive | negative
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(EmbeddingVariant, nullable=True)


class WorkflowAssignment(Base):
    __tablename__ = "workflow_assignments"

    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )


class IntentCursor(Base):
    """Per-(workflow, chat, thread) evaluation cursor + observability.

    A shared per-chat cursor would let one workflow (e.g. mid-throttle)
    consume messages another never got to evaluate; each advances alone.
    Stages a gate can record: evaluating_prefilter | prefilter_skip |
    throttled | evaluating | classifier_error | no_match | candidate |
    accumulating | suppressed | parked | rechecking | fired
    """

    __tablename__ = "intent_cursors"

    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    # thread_id or 0 for the main chat (NULL breaks the composite PK)
    thread_key: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=0)
    last_tg_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # Circuit breaker: stamped only on a SUCCESSFUL classifier call.
    last_llm_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # prefilter cosine
    last_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # classifier
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IntentEpisode(Base):
    """One occurrence of a workflow's intent in one thread — the first-class
    "topic" the pipeline tracks. While an episode is open the thread is
    sticky: every closed window goes to the classifier (prefilter bypassed),
    attributed to this episode or judged a new instance. After the agent
    runs, the episode carries what was done (execution_summary) so
    continuations of a handled topic are recognized and suppressed."""

    __tablename__ = "intent_episodes"
    __table_args__ = (
        Index("ix_intent_episodes_key_status", "workflow_id", "chat_id", "thread_key", "status"),
        Index("ix_intent_episodes_fingerprint", "workflow_id", "chat_id", "fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    thread_key: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(Text, default="candidate")
    # {"date": {"value": "...", "confidence": 0.9, "message_id": 123, "ts": "..."}}
    # Confidences decay lazily (see intent/state.py) — stored values are as-written.
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    # Rolling 1–2 sentence topic summary maintained by the classifier — the
    # evidence that survives cursor advances and bounded context windows.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    anchor_tg_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # sha256 of canonicalized sorted name=value pairs, set at convergence:
    # an exact-match fast path for duplicate suppression (semantic dedup is
    # the classifier's continuation verdict).
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the agent did, written when the linked AgentRun finishes; shown to
    # the classifier for post-fire windows.
    execution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Consecutive "unrelated" verdicts; candidates close at the configured k.
    unrelated_streak: Mapped[int] = mapped_column(Integer, default=0)
    # Cursor position when the episode parked as `converged` (cooldown): the
    # recheck runs only if messages arrived past this point.
    parked_at_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Bumped only by ATTRIBUTED activity (a verdict/slot update for this
    # episode) — unrelated chatter lets the idle clock run.
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PendingFire(Base):
    """Intent-to-fire record written BEFORE executing: a crash between
    deciding and acting can't double-create side effects."""

    __tablename__ = "pending_fires"
    __table_args__ = (
        Index("ix_pending_fires_status", "status", "id"),
        # At most one live fire per episode.
        Index(
            "uq_pending_fires_live_episode",
            "episode_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','confirm_wait','confirmed') AND episode_id IS NOT NULL"
            ),
            sqlite_where=text(
                "status IN ('pending','confirm_wait','confirmed') AND episode_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    thread_key: Mapped[int] = mapped_column(BigInteger, default=0)
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("intent_episodes.id", ondelete="SET NULL"), nullable=True
    )
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(Text, default="pending")
    confirm_nonce: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirm_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatEvalState(Base):
    """Per-chat cursor for the intent sweeper."""

    __tablename__ = "chat_eval_state"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    last_tg_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
