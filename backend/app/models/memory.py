from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base

# Dimension-less on purpose: pgvector's SQLAlchemy type VALIDATES dimensions
# client-side on every bind, which would reject writes the moment the
# re-embed job re-types the columns for a new model. The live dimension is
# owned by the DB typmod (set by migrations, changed by the swap DDL) and
# recorded in embedding_state. SQLite variant (JSON) exists only for unit
# tests and is dim-agnostic.
EmbeddingVariant = Vector().with_variant(JSON(), "sqlite")

IMPORT_STATUSES = ("pending", "validating", "ingesting", "done", "failed", "rejected")


class Chunk(Base):
    """A lull-delimited conversation segment. The embedded `text` is a render
    of the covered messages; retrieval re-renders from raw rows so edits only
    require re-embedding (stale=True), never data repair."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_chat", "chat_id"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # tg_message_id range covered (inclusive); per-chat monotonic.
    msg_tg_id_start: Mapped[int] = mapped_column(BigInteger)
    msg_tg_id_end: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(EmbeddingVariant, nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bumped on every edit that touches this chunk. The embed loop clears
    # stale only if the version is unchanged since it read the content, so an
    # edit landing mid-embed isn't overwritten with a pre-edit vector.
    content_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChunkState(Base):
    """Per-chat chunking cursor over tg_message_id order."""

    __tablename__ = "chunk_state"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    last_tg_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EmbeddingState(Base):
    """Singleton (id=1): which embedding model owns the stored vectors, and
    the progress of an in-flight model swap. Spec fields (prefixes) are
    persisted so a custom HF model survives restarts without a registry
    entry."""

    __tablename__ = "embedding_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    model_id: Mapped[str] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer)  # 0 = probe at swap time
    doc_prefix: Mapped[str] = mapped_column(Text, default="")
    query_prefix: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="ready")  # ready | reembedding
    # The requested swap, applied to the current fields only after the model
    # proves loadable — a bad custom id must never poison the live config.
    target: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    phase: Mapped[str | None] = mapped_column(Text, nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    messages_total: Mapped[int] = mapped_column(Integer, default=0)
    messages_ingested: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
