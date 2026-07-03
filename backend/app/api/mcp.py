import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.mcp_oauth import (
    OAuthFlowError,
    build_authorize_url,
    discover,
    exchange_code,
    register_client,
)
from app.core.config import get_settings
from app.core.crypto import decrypt, encrypt
from app.core.db import get_session
from app.core.security import require_operator
from app.models import Chat, ChatMcpServer, McpServer
from app.models.mcp import MCP_TRANSPORTS

log = logging.getLogger("convoke.mcp")

router = APIRouter(dependencies=[Depends(require_operator)])
# The provider's browser redirect can't carry operator auth — state is the secret.
public_router = APIRouter()


def _redirect_uri() -> str:
    return f"{get_settings().public_url.rstrip('/')}/api/mcp-oauth/callback"


class McpServerIn(BaseModel):
    name: str
    transport: str  # http | stdio
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    # e.g. {"Authorization": "Bearer …"}; None = keep existing on update
    headers: dict[str, str] | None = None
    enabled: bool = True
    auth_type: str = "none"  # none | oauth
    # Only for providers without dynamic client registration (e.g. Google).
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_scopes: str | None = None


class McpServerOut(BaseModel):
    id: int
    name: str
    transport: str
    url: str | None
    command: str | None
    args: list
    has_headers: bool
    enabled: bool
    auth_type: str
    oauth_status: str | None
    oauth_error: str | None
    created_at: datetime
    # Present right after registration/connect: open this in the browser.
    authorize_url: str | None = None


def _out(s: McpServer, authorize_url: str | None = None) -> McpServerOut:
    return McpServerOut(
        id=s.id,
        name=s.name,
        transport=s.transport,
        url=s.url,
        command=s.command,
        args=s.args or [],
        has_headers=s.headers_encrypted is not None,
        enabled=s.enabled,
        auth_type=s.auth_type or "none",
        oauth_status=s.oauth_status,
        oauth_error=s.oauth_error,
        created_at=s.created_at,
        authorize_url=authorize_url,
    )


def _validate(body: McpServerIn) -> None:
    if body.transport not in MCP_TRANSPORTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "transport must be http or stdio")
    if body.transport == "http" and not body.url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "http transport requires url")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "stdio transport requires command")


class McpTestIn(BaseModel):
    transport: str
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    bearer: str | None = None


class McpTestOut(BaseModel):
    ok: bool
    detail: str


@router.post("/mcp-servers/test", response_model=McpTestOut)
async def test_server_config(body: McpTestIn) -> McpTestOut:
    """Pre-registration probe: MCP handshake + tools/list against the form's
    values, so a typo'd URL or dead command is caught before saving."""
    from app.agents.mcp import build_probe_target, probe_target

    try:
        target = build_probe_target(
            body.transport,
            body.url,
            body.command,
            body.args,
            {"Authorization": f"Bearer {body.bearer}"} if body.bearer else None,
        )
    except ValueError as e:
        return McpTestOut(ok=False, detail=str(e))
    ok, detail = await probe_target(target)
    return McpTestOut(ok=ok, detail=detail)


@router.post("/mcp-servers/{server_id}/test", response_model=McpTestOut)
async def test_registered_server(
    server_id: int, session: AsyncSession = Depends(get_session)
) -> McpTestOut:
    """Probe a saved server with its stored credentials (incl. OAuth tokens)."""
    from app.agents.mcp import build_probe_target, ensure_oauth_token, probe_target

    server = await session.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server not found")
    headers = json.loads(decrypt(server.headers_encrypted)) if server.headers_encrypted else {}
    if server.auth_type == "oauth":
        try:
            token = await ensure_oauth_token(session, server)
        except Exception as e:  # noqa: BLE001 — surface as a test failure
            return McpTestOut(ok=False, detail=str(e))
        headers["Authorization"] = f"Bearer {token}"
    target = build_probe_target(
        server.transport, server.url, server.command, server.args or [], headers or None
    )
    ok, detail = await probe_target(target)
    return McpTestOut(ok=ok, detail=detail)


async def _start_oauth_flow(session: AsyncSession, server: McpServer) -> str:
    """Discovery → client (DCR or operator-provided) → authorize URL.
    Persists everything the callback will need; returns the URL to open."""
    async with httpx.AsyncClient(follow_redirects=True) as http:
        meta, resource = await discover(server.url, http)
        if not server.oauth_client_id:
            client_id, client_secret = await register_client(meta, _redirect_uri(), http)
            server.oauth_client_id = client_id
            server.oauth_client_secret_encrypted = encrypt(client_secret) if client_secret else None
    scopes = server.oauth_scopes or (" ".join(meta.scopes_supported) or None)
    url, state, verifier = build_authorize_url(
        meta, server.oauth_client_id, _redirect_uri(), resource, scopes
    )
    server.oauth_authorization_endpoint = meta.authorization_endpoint
    server.oauth_token_endpoint = meta.token_endpoint
    server.oauth_scopes = scopes
    server.oauth_resource = resource
    server.oauth_state = state
    server.oauth_pkce_verifier_encrypted = encrypt(verifier)
    server.oauth_status = "pending"
    server.oauth_error = None
    return url


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
        auth_type=body.auth_type if body.transport == "http" else "none",
        oauth_client_id=body.oauth_client_id or None,
        oauth_client_secret_encrypted=encrypt(body.oauth_client_secret)
        if body.oauth_client_secret
        else None,
        oauth_scopes=body.oauth_scopes or None,
    )
    session.add(server)
    await session.flush()

    authorize_url: str | None = None
    if server.auth_type == "oauth":
        server.enabled = False  # nothing works until the sign-in completes
        try:
            authorize_url = await _start_oauth_flow(session, server)
        except OAuthFlowError as e:
            server.oauth_status = "error"
            server.oauth_error = str(e)
        except Exception as e:  # noqa: BLE001 — surface, don't 500
            log.exception("oauth flow start failed for %s", server.name)
            server.oauth_status = "error"
            server.oauth_error = f"{type(e).__name__}: {str(e)[:200]}"
    await session.commit()
    return _out(server, authorize_url)


@router.post("/mcp-servers/{server_id}/connect", response_model=McpServerOut)
async def connect_server(server_id: int, session: AsyncSession = Depends(get_session)) -> McpServerOut:
    """(Re)start the OAuth sign-in for an existing server."""
    server = await session.get(McpServer, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server not found")
    if server.auth_type != "oauth" or not server.url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This server doesn't use OAuth")
    try:
        authorize_url = await _start_oauth_flow(session, server)
    except OAuthFlowError as e:
        server.oauth_status = "error"
        server.oauth_error = str(e)
        await session.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    await session.commit()
    return _out(server, authorize_url)


CALLBACK_PAGE = """<!doctype html>
<html><head><title>Convoke</title><style>
body {{ font-family: system-ui, sans-serif; display: grid; place-items: center;
       min-height: 100vh; margin: 0; background: #f4f6f9; color: #17222c; }}
div {{ text-align: center; }} p {{ color: #5a6b7a; }}
</style></head><body><div><h2>{title}</h2><p>{detail}</p></div></body></html>"""


@public_router.get("/mcp-oauth/callback")
async def oauth_callback(
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    server = (
        await session.execute(select(McpServer).where(McpServer.oauth_state == state))
    ).scalar_one_or_none() if state else None
    if server is None:
        return HTMLResponse(
            CALLBACK_PAGE.format(
                title="Sign-in link expired",
                detail="This authorization attempt is no longer active. Press Connect in Convoke to retry.",
            ),
            status_code=400,
        )
    # single-use regardless of outcome
    server.oauth_state = None
    verifier_enc, server.oauth_pkce_verifier_encrypted = server.oauth_pkce_verifier_encrypted, None

    if error or not code:
        server.oauth_status = "error"
        server.oauth_error = error_description or error or "Provider returned no code"
        await session.commit()
        return HTMLResponse(
            CALLBACK_PAGE.format(title="Sign-in failed", detail=server.oauth_error),
            status_code=400,
        )
    try:
        async with httpx.AsyncClient() as http:
            tokens = await exchange_code(
                server.oauth_token_endpoint,
                server.oauth_client_id,
                decrypt(server.oauth_client_secret_encrypted)
                if server.oauth_client_secret_encrypted
                else None,
                code,
                decrypt(verifier_enc),
                _redirect_uri(),
                server.oauth_resource,
                http,
            )
    except OAuthFlowError as e:
        server.oauth_status = "error"
        server.oauth_error = str(e)
        await session.commit()
        return HTMLResponse(
            CALLBACK_PAGE.format(title="Sign-in failed", detail=str(e)), status_code=400
        )

    server.oauth_access_token_encrypted = encrypt(tokens["access_token"])
    if tokens.get("refresh_token"):
        server.oauth_refresh_token_encrypted = encrypt(tokens["refresh_token"])
    server.oauth_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(tokens.get("expires_in", 3600))
    )
    server.oauth_status = "connected"
    server.oauth_error = None
    await session.commit()
    return HTMLResponse(
        CALLBACK_PAGE.format(
            title=f"{server.name} connected ✓",
            detail="You can close this tab — Convoke has what it needs.",
        )
    )


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
