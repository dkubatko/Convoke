from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base

MCP_TRANSPORTS = ("http", "stdio")
MCP_AUTH_TYPES = ("none", "oauth")  # bearer headers ride on headers_encrypted
OAUTH_STATUSES = ("pending", "connected", "error")


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

    # OAuth 2.1 (MCP authorization spec). One-time interactive sign-in from
    # the UI; Convoke stores encrypted tokens and refreshes them at run time.
    auth_type: Mapped[str] = mapped_column(Text, default="none")  # none | oauth
    oauth_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_authorization_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_token_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # RFC 8707 resource indicator — MCP requires binding tokens to the server URL.
    oauth_resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    # In-flight flow (single-use, unguessable).
    oauth_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_pkce_verifier_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
