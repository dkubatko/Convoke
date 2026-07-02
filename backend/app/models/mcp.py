from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base

MCP_TRANSPORTS = ("http", "stdio")


class McpServer(Base):
    """A registered MCP server. Streamable HTTP preferred (run servers as
    their own compose services); stdio supported but the command must exist
    inside the backend image."""

    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    transport: Mapped[str] = mapped_column(Text)  # http | stdio
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    args: Mapped[list] = mapped_column(JSON, default=list)
    # Encrypted JSON dict of HTTP headers (e.g. {"Authorization": "Bearer …"}).
    headers_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatMcpServer(Base):
    """Per-chat enablement: which MCP servers this chat's agent may use."""

    __tablename__ = "chat_mcp_servers"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    mcp_server_id: Mapped[int] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True
    )
