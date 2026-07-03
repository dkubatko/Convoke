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

# 384 matches intfloat/multilingual-e5-small; changing models means a
# migration + full re-embed. SQLite variant (JSON) exists only for unit tests.
EMBEDDING_DIM = 384
EmbeddingVariant = Vector(EMBEDDING_DIM).with_variant(JSON(), "sqlite")

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
