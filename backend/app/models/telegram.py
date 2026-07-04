from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Status values are plain text columns (not native enums) to keep migrations trivial.
BOT_STATUSES = ("active", "disabled", "error")
CHAT_STATUSES = ("pending_auth", "authorized", "left")
MESSAGE_SOURCES = ("live", "import", "self")

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class Bot(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_bot_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    token_encrypted: Mapped[str] = mapped_column(Text)
    # False means Telegram privacy mode is ON and the bot cannot see regular
    # group messages — Convoke's memory premise is broken until fixed.
    can_read_all_group_messages: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(Text, default="active")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # getUpdates offset: next update_id to request. Advanced only after the
    # updates are persisted to the inbox (persist-then-ack).
    next_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    # Telegram holds updates only 24h — downtime beyond that is permanent,
    # undetectable message loss. Tracked to mark memory gaps on restart.
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (UniqueConstraint("bot_id", "tg_chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(Text)  # group | supergroup
    title: Mapped[str] = mapped_column(Text, default="")
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(Text, default="pending_auth")
    authorized_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    authorized_by_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InboxUpdate(Base):
    """Transactional inbox: raw Telegram updates, persisted before the
    getUpdates offset is advanced. Downstream consumers are crash-safe."""

    __tablename__ = "updates_inbox"
    __table_args__ = (
        UniqueConstraint("bot_id", "update_id"),
        Index("ix_updates_inbox_unprocessed", "id", postgresql_where="processed_at IS NULL"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    update_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONVariant)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "tg_message_id"),
        Index("ix_messages_chat_sent", "chat_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The message this one replies to — lets the intent classifier pull the
    # quoted original into context even when it's far back in the history.
    reply_to_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(Text, default="live")  # live | import | self
    # Provenance: which import brought this message in — makes a poisoned or
    # wrong-chat import surgically deletable.
    import_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MemoryGap(Base):
    """A known hole in chat memory (worker down >24h — Telegram discarded the
    updates). Surfaced to the operator and mentioned in agent context."""

    __tablename__ = "memory_gaps"
    __table_args__ = (Index("ix_memory_gaps_chat", "chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gap_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuthNonce(Base):
    """Server-stored nonce backing the in-group 'Authorize Convoke' button.
    callback_data carries only this nonce — never authority."""

    __tablename__ = "auth_nonces"

    id: Mapped[int] = mapped_column(primary_key=True)
    nonce: Mapped[str] = mapped_column(Text, unique=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(Text, default="authorize")
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
