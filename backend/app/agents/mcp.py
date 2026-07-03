"""MCP toolsets for agent runs, built per run from the chat's enabled servers.

Connections open lazily inside the run (AsyncExitStack in execute_run) and
close when it finishes — configured servers cost nothing while idle.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import httpx
from pydantic_ai.mcp import MCPToolset, StdioTransport, StreamableHttpTransport
from pydantic_ai.toolsets import WrapperToolset
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
    toolset = MCPToolset(transport, id=f"mcp_{server.id}")
    # sanitize BEFORE prefixing: aggregator tool names may contain characters
    # the model API rejects (Smithery: 'server:tool')
    return SanitizedToolset(toolset).prefixed(_safe_prefix(server.name))


def _safe_prefix(name: str) -> str:
    """Prefix tool names per server so two servers' tools can't collide."""
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower())[:24] or "mcp"


def sanitize_tool_name(name: str) -> str:
    """OpenAI-compatible tool names must match ^[a-zA-Z0-9_-]+$; aggregators
    like Smithery expose namespaced names ('server:tool') that violate it."""
    return "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "_-")) else "_" for ch in name) or "tool"


@dataclass
class SanitizedToolset(WrapperToolset):
    """Renames tools to model-safe names and routes calls back to the
    originals. Mirrors PrefixedToolset's mechanics."""

    _to_original: dict[str, str] = field(default_factory=dict)

    async def get_tools(self, ctx):
        out = {}
        self._to_original.clear()
        for name, tool in (await super().get_tools(ctx)).items():
            safe = sanitize_tool_name(name)
            while safe in out and self._to_original.get(safe) != name:
                safe += "_"
            self._to_original[safe] = name
            out[safe] = replace(tool, toolset=self, tool_def=replace(tool.tool_def, name=safe))
        return out

    async def call_tool(self, name, tool_args, ctx, tool):
        original = self._to_original.get(name, name)
        ctx = replace(ctx, tool_name=original)
        tool = replace(tool, tool_def=replace(tool.tool_def, name=original))
        return await super().call_tool(original, tool_args, ctx, tool)


PROBE_TIMEOUT_S = 15


def build_probe_target(
    transport: str,
    url: str | None,
    command: str | None,
    args: list[str] | None,
    headers: dict | None,
):
    if transport == "http":
        return StreamableHttpTransport(url, headers=headers or None)
    if transport == "stdio":
        return StdioTransport(command, list(args or []))
    raise ValueError(f"Unknown MCP transport {transport!r}")


async def probe_target(target) -> tuple[bool, str]:
    """Full MCP handshake + tools/list — proves the server is real before it
    can be saved. Returns (ok, human-readable detail)."""
    import asyncio

    from fastmcp import Client

    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            async with Client(target) as client:
                tools = await client.list_tools()
    except TimeoutError:
        return False, f"No MCP handshake within {PROBE_TIMEOUT_S}s — is this actually an MCP endpoint?"
    except Exception as e:  # noqa: BLE001 — every failure becomes direction
        detail = str(e) or type(e).__name__
        lowered = detail.lower()
        if "401" in detail or "unauthorized" in lowered:
            return False, "The server rejected the credentials (401) — check the bearer token."
        if "404" in detail or "not found" in lowered:
            return False, "The URL answered but not with MCP (404) — check the path (usually /mcp)."
        if "connect" in lowered or "connection" in lowered or "name or service" in lowered:
            return False, (
                "Couldn't reach the server — check the URL (from Docker, host services "
                "are at http://host.docker.internal)."
            )
        if "no such file" in lowered or "not found" in lowered:
            return False, "The command doesn't exist inside the backend container."
        return False, f"{type(e).__name__}: {detail[:200]}"
    names = sorted(t.name for t in tools)
    shown = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
    return True, f"Reachable — {len(names)} tool{'s' if len(names) != 1 else ''}: {shown}"


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
