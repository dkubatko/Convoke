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
from sqlalchemy import Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# Status values are plain text columns (not native enums) to keep migrations trivial.
BOT_STATUSES = ("active", "disabled", "error")
CHAT_STATUSES = ("pending_auth", "authorized", "left")
MESSAGE_SOURCES = ("live", "import", "self")
ATTACHMENT_KINDS = (
    "photo", "video", "animation", "voice", "video_note", "sticker", "image_document", "audio",
    "file",
)
# Messages.content["type"]: Telegram's non-file content, stored structured and
# rendered at read time (polls have their own table — see Poll).
CONTENT_TYPES = (
    "location", "venue", "contact", "dice", "checklist", "story", "game", "invoice",
    "giveaway", "paid_media",
)
ATTACHMENT_STATUSES = ("pending", "described", "failed", "skipped")

JSONVariant = JSON().with_variant(JSONB(), "postgresql")
# For nullable JSON columns: None is SQL NULL, not a stored JSON 'null'.
NullableJSON = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


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
    # Operator-written identity (who the bot is in this chat). NULL = the
    # default (app.agents.identity.DEFAULT_IDENTITY); the hidden system rules
    # are appended to either.
    identity: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatThread(Base):
    """A thread (forum topic or reply-thread) the bot has learned about in a
    chat. thread_key = message.thread_id or 0 (0 = the General/main thread).

    Rows exist only for threads with a captured/assigned title OR a non-default
    monitored flag — a thread with no row is monitored and unnamed, so the
    default (all threads monitored) needs no rows. `title` is captured from
    forum-topic service events or assigned by an operator; the Bot API cannot
    read existing topic names, so pre-existing topics are named by hand."""

    __tablename__ = "chat_threads"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    thread_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChatMember(Base):
    """Identity of a chat participant, keyed by their stable Telegram user id.

    Every model-facing render resolves a message's sender through this row, so
    the same person reads consistently across imported history and live traffic
    no matter what name each individual message carried.

    - `auto_name`: the latest observed display name (raw, from Telegram or the
      export). `name_basis_at` is the sent_at of the message that set it, so a
      later import of OLD history never clobbers a newer live-derived name.
    - `override_name`: an operator/agent-set label; wins over auto_name.
    - `handle`: the @username, captured from live messages when present (the
      export carries none); null otherwise.
    """

    __tablename__ = "chat_members"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    sender_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    auto_name: Mapped[str] = mapped_column(Text, default="")
    override_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Treat this member's messages as bot output: tagged [bot] in every render,
    # excluded from what memory search SCORES (embedding input) while staying in
    # chunk text and directly readable. Auto-true for the chat's own bot account
    # (migration backfill); operator-set for other bots — Telegram exports carry
    # no bot flag, so they can't be auto-detected.
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    name_basis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def display_name(self) -> str:
        return self.override_name or self.auto_name or "Unknown"


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
    # Structured non-file content ({"type": CONTENT_TYPES, ...}): locations,
    # contacts, checklists… Kept as data, not baked into text, so mutable
    # content (live locations, checklist ticks) updates in place and every
    # renderer reads the current state through message_body().
    content: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Loaded eagerly (selectin) so every renderer can annotate media without
    # signature changes; the vast majority of messages have no attachment.
    attachment: Mapped["MessageAttachment | None"] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True
    )
    # Eager like attachment; message_body treats it as None when unloaded (a
    # row built in code without one), so it never lazy-loads.
    poll: Mapped["Poll | None"] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True
    )


class MessageAttachment(Base):
    """The media item carried by a message (Telegram: at most one per message;
    albums arrive as one message per item, linked by media_group_id).

    Bytes are never persisted — the understanding worker downloads them
    transiently and stores only text (description/transcript). file_id lets
    live media be re-fetched from Telegram for future re-description;
    import-sourced media has file_id=NULL (describe-then-discard)."""

    __tablename__ = "message_attachments"
    __table_args__ = (
        Index("ix_attachments_chat_msg", "chat_id", "tg_message_id"),
        Index("ix_attachments_pending", "id", postgresql_where="status = 'pending'"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), unique=True
    )
    # Denormalized so the worker scans and view-by-tg-id lookups skip a join.
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(Text)  # ATTACHMENT_KINDS
    file_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL: import-sourced
    # Import-sourced media: file location relative to imports_dir, held only
    # until described, then deleted (describe-then-discard).
    import_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_unique_id: Mapped[str] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumb_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # album id, opaque
    sticker_emoji: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)  # "file" kind
    description: Mapped[str | None] = mapped_column(Text, nullable=True)  # vision output
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)  # transcription output
    status: Mapped[str] = mapped_column(Text, default="pending")  # ATTACHMENT_STATUSES
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    described_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Poll(Base):
    """A native Telegram poll carried by a message.

    Votes reach bots only for polls the bot itself sent (`poll` snapshots and,
    when not anonymous, per-user `poll_answer`s), plus a final `poll` snapshot
    when a poll is stopped. For members' polls Telegram never reports votes,
    so counts stay unknown (counts_at NULL) rather than a misleading zero.
    Updates are keyed by tg_poll_id alone — not unique, since a forwarded poll
    is the same poll in every chat it lands in."""

    __tablename__ = "polls"
    __table_args__ = (Index("ix_polls_tg_poll_id", "tg_poll_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), unique=True
    )
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    tg_poll_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL: imported
    question: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"id": persistent option id, "text": str, "votes": int | None}]
    options: Mapped[list] = mapped_column(JSONVariant)
    type: Mapped[str] = mapped_column(Text, default="regular")  # regular | quiz
    # NULL when the source doesn't say (history exports omit these flags).
    multiple_answers: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    anonymous: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sent_by_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    close_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_voters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When the vote counts were last known to be true; NULL = never reported.
    counts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    votes: Mapped[list["PollVote"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True,
        order_by="PollVote.voted_at",
    )


class PollVote(Base):
    """One voter's current choice in a non-anonymous poll the bot sent. A
    changed vote replaces the row; a retracted vote deletes it."""

    __tablename__ = "poll_votes"

    poll_id: Mapped[int] = mapped_column(
        ForeignKey("polls.id", ondelete="CASCADE"), primary_key=True
    )
    voter_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # user or chat id
    voter_name: Mapped[str] = mapped_column(Text, default="")
    option_ids: Mapped[list] = mapped_column(JSONVariant)  # persistent option ids
    voted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
