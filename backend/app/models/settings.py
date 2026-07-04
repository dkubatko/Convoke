from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RuntimeSetting(Base):
    """Global operator override for a tunable, keyed by its Settings field name.
    Only genuine deviations from the default are stored."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer)


class ChatSetting(Base):
    """Per-chat override for a chat-scoped tunable, falling back to the global
    default when absent."""

    __tablename__ = "chat_settings"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[int] = mapped_column(Integer)
