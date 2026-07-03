import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.agents.mcp_oauth import (
    AuthServerMeta,
    OAuthFlowError,
    build_authorize_url,
    discover,
    exchange_code,
    refresh_tokens,
    register_client,
)

MCP_URL = "https://rs.example/mcp"
AS = "https://auth.example"


def provider(handlers):
    """httpx client with a mock provider defined as {(method, url): responder}."""

    def handle(request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url).split("?")[0])
        if key in handlers:
            return handlers[key](request)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


AS_METADATA = {
    "issuer": AS,
    "authorization_endpoint": f"{AS}/authorize",
    "token_endpoint": f"{AS}/token",
    "registration_endpoint": f"{AS}/register",
    "scopes_supported": ["calendar.read", "calendar.write"],
}


async def test_discover_via_protected_resource_metadata():
    handlers = {
        ("GET", "https://rs.example/.well-known/oauth-protected-resource/mcp"): lambda r: httpx.Response(
            200, json={"authorization_servers": [AS], "resource": MCP_URL}
        ),
        ("GET", f"{AS}/.well-known/oauth-authorization-server"): lambda r: httpx.Response(
            200, json=AS_METADATA
        ),
    }
    async with provider(handlers) as http:
        meta, resource = await discover(MCP_URL, http)
    assert meta.token_endpoint == f"{AS}/token"
    assert resource == MCP_URL
    assert meta.scopes_supported == ["calendar.read", "calendar.write"]


async def test_discover_falls_back_to_mcp_host_as_auth_server():
    handlers = {
        ("GET", "https://rs.example/.well-known/oauth-authorization-server"): lambda r: httpx.Response(
            200, json={**AS_METADATA, "issuer": "https://rs.example"}
        ),
    }
    async with provider(handlers) as http:
        meta, resource = await discover(MCP_URL, http)
    assert meta.issuer == "https://rs.example"
    assert resource == MCP_URL


async def test_discover_no_oauth_anywhere_raises_readable_error():
    async with provider({}) as http:
        with pytest.raises(OAuthFlowError, match="discover"):
            await discover(MCP_URL, http)


async def test_register_client_dcr():
    def register(request):
        body = json.loads(request.content)
        assert "https://convoke.example/api/mcp-oauth/callback" in body["redirect_uris"]
        return httpx.Response(201, json={"client_id": "abc123"})

    meta = AuthServerMeta(AS, f"{AS}/authorize", f"{AS}/token", f"{AS}/register", [])
    async with provider({("POST", f"{AS}/register"): register}) as http:
        client_id, secret = await register_client(
            meta, "https://convoke.example/api/mcp-oauth/callback", http
        )
    assert client_id == "abc123" and secret is None


async def test_register_client_without_dcr_directs_to_manual_setup():
    meta = AuthServerMeta(AS, f"{AS}/authorize", f"{AS}/token", None, [])
    async with provider({}) as http:
        with pytest.raises(OAuthFlowError, match="client id"):
            await register_client(meta, "https://x/cb", http)


def test_authorize_url_has_valid_pkce_state_and_resource():
    meta = AuthServerMeta(AS, f"{AS}/authorize", f"{AS}/token", None, [])
    url, state, verifier = build_authorize_url(meta, "cid", "https://x/cb", MCP_URL, "calendar.write")
    q = parse_qs(urlsplit(url).query)
    assert q["state"] == [state]
    assert q["resource"] == [MCP_URL]
    assert q["code_challenge_method"] == ["S256"]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [expected]
    assert q["scope"] == ["calendar.write"]


async def test_exchange_code_sends_verifier_and_resource():
    def token(request):
        form = parse_qs(request.content.decode())
        assert form["code_verifier"] == ["ver123"]
        assert form["resource"] == [MCP_URL]
        assert form["grant_type"] == ["authorization_code"]
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    async with provider({("POST", f"{AS}/token"): token}) as http:
        tokens = await exchange_code(
            f"{AS}/token", "cid", None, "code123", "ver123", "https://x/cb", MCP_URL, http
        )
    assert tokens["access_token"] == "at"


async def test_refresh_tokens_roundtrip():
    def token(request):
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["rt0"]
        return httpx.Response(200, json={"access_token": "at2", "expires_in": 900})

    async with provider({("POST", f"{AS}/token"): token}) as http:
        tokens = await refresh_tokens(f"{AS}/token", "cid", "sec", "rt0", MCP_URL, http)
    assert tokens["access_token"] == "at2"


async def test_token_error_is_readable():
    handlers = {("POST", f"{AS}/token"): lambda r: httpx.Response(400, json={"error": "invalid_grant"})}
    async with provider(handlers) as http:
        with pytest.raises(OAuthFlowError, match="invalid_grant"):
            await refresh_tokens(f"{AS}/token", "cid", None, "rt0", None, http)
