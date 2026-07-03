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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base
from app.models.memory import EmbeddingVariant

WORKFLOW_TYPES = ("scheduled", "intent")
EXAMPLES_STATUSES = ("pending", "ready", "fallback")
FIRE_STATUSES = ("pending", "confirm_wait", "confirmed", "done", "cancelled", "error")


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
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=3600)
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


class TriggerState(Base):
    """Convergence state per (workflow, chat, thread): slots accumulate
    across evaluation windows until required slots are confidently filled."""

    __tablename__ = "trigger_states"
    __table_args__ = (UniqueConstraint("workflow_id", "chat_id", "thread_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    # thread_id or 0 for the main chat (NULL breaks the unique constraint)
    thread_key: Mapped[int] = mapped_column(BigInteger, default=0)
    # {"date": {"value": "...", "confidence": 0.9, "message_id": 123, "ts": "..."}}
    slots: Mapped[dict] = mapped_column(JSON, default=dict)
    last_match_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_llm_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Observability: where the most recent evaluation of this state ended.
    # Stages: cooldown | prefilter_skip | throttled | classifier_error |
    #         no_match | accumulating | fired
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # prefilter cosine
    last_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # classifier
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PendingFire(Base):
    """Intent-to-fire record written BEFORE executing: a crash between
    deciding and acting can't double-create side effects."""

    __tablename__ = "pending_fires"
    __table_args__ = (Index("ix_pending_fires_status", "status", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    thread_key: Mapped[int] = mapped_column(BigInteger, default=0)
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
