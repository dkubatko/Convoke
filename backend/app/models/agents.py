from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.memory import EmbeddingVariant

PROVIDER_ROLES = ("embeddings", "intent", "agent")
RUN_STATUSES = ("pending", "running", "done", "error")


class ModelProvider(Base):
    """One OpenAI-compatible endpoint per role: cheap model for intent
    classification, strong model for agent runs, optional embeddings override."""

    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(Text, unique=True)
    base_url: Mapped[str] = mapped_column(Text)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
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
    trigger_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    request_text: Mapped[str] = mapped_column(Text, default="")
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
