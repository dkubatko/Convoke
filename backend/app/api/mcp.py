import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt
from app.core.db import get_session
from app.core.security import require_operator
from app.models import Chat, ChatMcpServer, McpServer
from app.models.mcp import MCP_TRANSPORTS

router = APIRouter(dependencies=[Depends(require_operator)])


class McpServerIn(BaseModel):
    name: str
    transport: str  # http | stdio
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    # e.g. {"Authorization": "Bearer …"}; None = keep existing on update
    headers: dict[str, str] | None = None
    enabled: bool = True


class McpServerOut(BaseModel):
    id: int
    name: str
    transport: str
    url: str | None
    command: str | None
    args: list
    has_headers: bool
    enabled: bool
    created_at: datetime


def _out(s: McpServer) -> McpServerOut:
    return McpServerOut(
        id=s.id,
        name=s.name,
        transport=s.transport,
        url=s.url,
        command=s.command,
        args=s.args or [],
        has_headers=s.headers_encrypted is not None,
        enabled=s.enabled,
        created_at=s.created_at,
    )


def _validate(body: McpServerIn) -> None:
    if body.transport not in MCP_TRANSPORTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "transport must be http or stdio")
    if body.transport == "http" and not body.url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "http transport requires url")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "stdio transport requires command")


@router.post("/mcp-servers", response_model=McpServerOut, status_code=status.HTTP_201_CREATED)
async def create_server(body: McpServerIn, session: AsyncSession = Depends(get_session)) -> McpServerOut:
    _validate(body)
    server = McpServer(
        name=body.name,
        transport=body.transport,
        url=body.url,
        command=body.command,
        args=body.args,
        headers_encrypted=encrypt(json.dumps(body.headers)) if body.headers else None,
        enabled=body.enabled,
    )
    session.add(server)
    await session.commit()
    return _out(server)


@router.get("/mcp-servers", response_model=list[McpServerOut])
async def list_servers(session: AsyncSession = Depends(get_session)) -> list[McpServerOut]:
    rows = (await session.execute(select(McpServer).order_by(McpServer.id))).scalars()
    return [_out(s) for s in rows]


@router.put("/mcp-servers/{server_id}", response_model=McpServerOut)
async def update_server(
    server_id: int, body: McpServerIn, session: AsyncSession = Depends(get_session)
) -> McpServerOut:
    _validate(body)
    server = await session.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server not found")
    server.name = body.name
    server.transport = body.transport
    server.url = body.url
    server.command = body.command
    server.args = body.args
    server.enabled = body.enabled
    if body.headers is not None:
        server.headers_encrypted = encrypt(json.dumps(body.headers)) if body.headers else None
    await session.commit()
    return _out(server)


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: int, session: AsyncSession = Depends(get_session)) -> None:
    server = await session.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server not found")
    await session.delete(server)
    await session.commit()


@router.get("/chats/{chat_id}/mcp", response_model=list[int])
async def chat_servers(chat_id: int, session: AsyncSession = Depends(get_session)) -> list[int]:
    if await session.get(Chat, chat_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    return list(
        (
            await session.execute(
                select(ChatMcpServer.mcp_server_id).where(ChatMcpServer.chat_id == chat_id)
            )
        ).scalars()
    )


@router.put("/chats/{chat_id}/mcp", response_model=list[int])
async def set_chat_servers(
    chat_id: int, server_ids: list[int], session: AsyncSession = Depends(get_session)
) -> list[int]:
    if await session.get(Chat, chat_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    await session.execute(delete(ChatMcpServer).where(ChatMcpServer.chat_id == chat_id))
    for sid in set(server_ids):
        if await session.get(McpServer, sid) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown server id {sid}")
        session.add(ChatMcpServer(chat_id=chat_id, mcp_server_id=sid))
    await session.commit()
    return sorted(set(server_ids))
