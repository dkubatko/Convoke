"""MCP toolsets for agent runs, built per run from the chat's enabled servers.

Connections open lazily inside the run (AsyncExitStack in execute_run) and
close when it finishes — configured servers cost nothing while idle.
"""

import json
import logging

from pydantic_ai.mcp import MCPToolset, StdioTransport, StreamableHttpTransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.models.mcp import ChatMcpServer, McpServer

log = logging.getLogger("convoke.mcp")


def build_toolset(server: McpServer) -> MCPToolset:
    if server.transport == "http":
        headers = json.loads(decrypt(server.headers_encrypted)) if server.headers_encrypted else None
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
            toolsets.append(build_toolset(s))
        except Exception:  # noqa: BLE001 — one bad server must not kill the run
            log.exception("could not build toolset for MCP server %s", s.name)
    return toolsets
