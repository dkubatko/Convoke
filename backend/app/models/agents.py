from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.memory import EmbeddingVariant
from app.models.telegram import JSONVariant

RUN_STATUSES = ("pending", "running", "done", "declined", "error")

# Execution roles a connected model can be assigned to. Embeddings is not a
# role: chat memory embeds locally (sentence-transformers) by design.
MODEL_ROLES = ("agent", "intent", "vision", "transcription", "video")

# The capability a model must have for the role to work. The UI warns (but
# does not block) when the assigned model lacks it.
ROLE_REQUIRED_CAPABILITY = {
    "agent": "chat",
    "intent": "chat",
    "vision": "vision",
    "transcription": "transcription",
    "video": "video",
}

# Attachment kinds each media role unlocks — used to requeue skipped
# attachments when a model is (re)assigned.
ROLE_ATTACHMENT_KINDS = {
    "vision": ("photo", "sticker", "image_document", "video", "video_note"),
    "transcription": ("voice", "audio", "video", "video_note"),
    "video": ("video", "video_note"),
}


class ConnectedModel(Base):
    """An operator-connected OpenAI-compatible endpoint in the model library.
    Execution roles resolve to a model via ModelRoleAssignment."""

    __tablename__ = "connected_models"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    base_url: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(Text)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {"chat": bool, "vision": bool, "transcription": bool, "video": bool} —
    # chat/vision/transcription come from probes; video is operator-declared.
    capabilities: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelRoleAssignment(Base):
    """role → connected model. RESTRICT: deleting an assigned model 409s in
    the API with the list of roles still pointing at it."""

    __tablename__ = "model_role_assignments"

    role: Mapped[str] = mapped_column(Text, primary_key=True)  # MODEL_ROLES
    model_id: Mapped[int] = mapped_column(ForeignKey("connected_models.id", ondelete="RESTRICT"))
    # Reasoning effort sent with this role's calls (low/medium/high or any
    # provider-specific string via Custom). NULL = Default: the parameter is
    # OMITTED entirely — reasoning models reason at their own default, and
    # non-reasoning models never see an unknown parameter. Validated with a
    # live micro-call at assignment time; per-role because the same model can
    # serve agent (wants effort) and intent (wants latency).
    reasoning_effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Note(Base):
    """Agent-writable durable memory, per chat. Keyed notes upsert; all notes
    embed for semantic recall. Soft-deleted, never physically removed by the
    agent itself."""

    __tablename__ = "notes"
    __table_args__ = (Index("ix_notes_chat", "chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    key: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(EmbeddingVariant, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentRun(Base):
    """Audit log + work queue for agent invocations."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_pending", "status", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    trigger: Mapped[str] = mapped_column(Text)  # mention | reply | workflow
    # Set when trigger == 'workflow': which workflow queued this run.
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    trigger_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    request_text: Mapped[str] = mapped_column(Text, default="")
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tools the agent called during the run, in order: each
    # {"tool": name, "args": <truncated json>, "ok": bool}. Null on old rows /
    # runs that predate capture; [] means the agent called no tools.
    tool_calls: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
