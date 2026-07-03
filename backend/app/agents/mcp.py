"""MCP toolsets for agent runs, built per run from the chat's enabled servers.

Connections open lazily inside the run (AsyncExitStack in execute_run) and
close when it finishes — configured servers cost nothing while idle.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from pydantic_ai.mcp import MCPToolset, StdioTransport, StreamableHttpTransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.mcp_oauth import OAuthFlowError, refresh_tokens
from app.core.crypto import decrypt, encrypt
from app.models.mcp import ChatMcpServer, McpServer

log = logging.getLogger("convoke.mcp")

TOKEN_REFRESH_MARGIN = timedelta(seconds=60)


async def ensure_oauth_token(session: AsyncSession, server: McpServer) -> str:
    """Returns a live access token, refreshing (and persisting) if needed."""
    if server.oauth_status != "connected" or not server.oauth_access_token_encrypted:
        raise OAuthFlowError(f"{server.name} needs an OAuth sign-in (Tools page → Connect)")
    expires_at = server.oauth_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is None or expires_at > datetime.now(timezone.utc) + TOKEN_REFRESH_MARGIN:
        return decrypt(server.oauth_access_token_encrypted)
    if not server.oauth_refresh_token_encrypted:
        server.oauth_status = "error"
        server.oauth_error = "Access token expired and no refresh token was issued — sign in again."
        await session.commit()
        raise OAuthFlowError(server.oauth_error)
    async with httpx.AsyncClient() as http:
        tokens = await refresh_tokens(
            server.oauth_token_endpoint,
            server.oauth_client_id,
            decrypt(server.oauth_client_secret_encrypted)
            if server.oauth_client_secret_encrypted
            else None,
            decrypt(server.oauth_refresh_token_encrypted),
            server.oauth_resource,
            http,
        )
    server.oauth_access_token_encrypted = encrypt(tokens["access_token"])
    if tokens.get("refresh_token"):
        server.oauth_refresh_token_encrypted = encrypt(tokens["refresh_token"])
    server.oauth_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(tokens.get("expires_in", 3600))
    )
    await session.commit()
    return tokens["access_token"]


def build_toolset(server: McpServer, extra_headers: dict | None = None) -> MCPToolset:
    if server.transport == "http":
        headers = json.loads(decrypt(server.headers_encrypted)) if server.headers_encrypted else {}
        headers = {**headers, **(extra_headers or {})} or None
        transport = StreamableHttpTransport(server.url, headers=headers)
    elif server.transport == "stdio":
        transport = StdioTransport(server.command, list(server.args or []))
    else:
        raise ValueError(f"Unknown MCP transport {server.transport!r}")
    return MCPToolset(transport, id=f"mcp_{server.id}").prefixed(_safe_prefix(server.name))


def _safe_prefix(name: str) -> str:
    """Prefix tool names per server so two servers' tools can't collide."""
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower())[:24] or "mcp"


async def toolsets_for_chat(session: AsyncSession, chat_id: int) -> list[MCPToolset]:
    servers = (
        (
            await session.execute(
                select(McpServer)
                .join(ChatMcpServer, ChatMcpServer.mcp_server_id == McpServer.id)
                .where(ChatMcpServer.chat_id == chat_id, McpServer.enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    toolsets = []
    for s in servers:
        try:
            extra_headers = None
            if s.auth_type == "oauth":
                token = await ensure_oauth_token(session, s)
                extra_headers = {"Authorization": f"Bearer {token}"}
            toolsets.append(build_toolset(s, extra_headers))
        except Exception:  # noqa: BLE001 — one bad server must not kill the run
            log.exception("could not build toolset for MCP server %s", s.name)
    return toolsets
