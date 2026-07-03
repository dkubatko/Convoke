"""OAuth 2.1 client flow for MCP servers (MCP authorization spec).

The dance, in order:
1. **Discovery** — the MCP server's protected-resource metadata (RFC 9728)
   names its authorization server; that server's metadata (RFC 8414) gives
   the authorize/token/registration endpoints.
2. **Client registration** — dynamic (RFC 7591) when the provider supports
   it; otherwise the operator supplies a client id/secret they created in
   the provider's console (e.g. Google).
3. **Authorization** — the operator's browser visits the authorize URL
   (PKCE S256 + unguessable state + RFC 8707 resource binding) and signs in
   once; the provider redirects to Convoke's callback.
4. **Exchange / refresh** — code → tokens at the token endpoint; refresh
   happens transparently at agent-run time.

Everything here is pure protocol: httpx in, dicts out. Persistence and
routing live in the API layer.
"""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx

DISCOVERY_TIMEOUT_S = 15


class OAuthFlowError(RuntimeError):
    """Human-readable failure — shown verbatim to the operator."""


@dataclass
class AuthServerMeta:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    scopes_supported: list[str]


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


async def discover(mcp_url: str, http: httpx.AsyncClient) -> tuple[AuthServerMeta, str]:
    """Returns (authorization server metadata, canonical resource URL)."""
    origin = _origin(mcp_url)
    path = urlsplit(mcp_url).path.rstrip("/")

    resource_meta = None
    for candidate in (
        f"{origin}/.well-known/oauth-protected-resource{path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        try:
            resp = await http.get(candidate, timeout=DISCOVERY_TIMEOUT_S)
            if resp.status_code == 200:
                resource_meta = resp.json()
                break
        except httpx.HTTPError:
            continue

    if resource_meta is not None:
        servers = resource_meta.get("authorization_servers") or []
        if not servers:
            raise OAuthFlowError(
                "The server publishes protected-resource metadata but names no "
                "authorization server."
            )
        auth_server = servers[0]
        resource = resource_meta.get("resource") or mcp_url
    else:
        # Pre-RFC9728 servers: the MCP host doubles as the authorization server.
        auth_server = origin
        resource = mcp_url

    meta = await _auth_server_metadata(auth_server, http)
    return meta, resource


async def _auth_server_metadata(auth_server: str, http: httpx.AsyncClient) -> AuthServerMeta:
    base = auth_server.rstrip("/")
    parts = urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    issuer_path = parts.path.rstrip("/")
    candidates = [
        f"{origin}/.well-known/oauth-authorization-server{issuer_path}",
        f"{origin}/.well-known/oauth-authorization-server",
        f"{origin}/.well-known/openid-configuration{issuer_path}",
        f"{origin}/.well-known/openid-configuration",
    ]
    for candidate in candidates:
        try:
            resp = await http.get(candidate, timeout=DISCOVERY_TIMEOUT_S)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        data = resp.json()
        if "authorization_endpoint" in data and "token_endpoint" in data:
            return AuthServerMeta(
                issuer=data.get("issuer", base),
                authorization_endpoint=data["authorization_endpoint"],
                token_endpoint=data["token_endpoint"],
                registration_endpoint=data.get("registration_endpoint"),
                scopes_supported=data.get("scopes_supported") or [],
            )
    raise OAuthFlowError(
        f"Couldn't discover OAuth metadata for {auth_server} — the server may not "
        "support OAuth, or it isn't an MCP authorization server."
    )


async def register_client(
    meta: AuthServerMeta, redirect_uri: str, http: httpx.AsyncClient
) -> tuple[str, str | None]:
    """Dynamic client registration. Raises with direction when unsupported."""
    if not meta.registration_endpoint:
        raise OAuthFlowError(
            "This provider doesn't support automatic client registration — create an "
            "OAuth client in its console and enter the client id/secret here "
            f"(redirect URI: {redirect_uri})."
        )
    resp = await http.post(
        meta.registration_endpoint,
        json={
            "client_name": "Convoke",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=DISCOVERY_TIMEOUT_S,
    )
    if resp.status_code not in (200, 201):
        raise OAuthFlowError(f"Client registration failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    return data["client_id"], data.get("client_secret")


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def build_authorize_url(
    meta: AuthServerMeta,
    client_id: str,
    redirect_uri: str,
    resource: str,
    scopes: str | None,
) -> tuple[str, str, str]:
    """Returns (url, state, pkce_verifier)."""
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    if scopes:
        params["scope"] = scopes
        # providers that hand out refresh tokens only when asked (Google)
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    sep = "&" if "?" in meta.authorization_endpoint else "?"
    return f"{meta.authorization_endpoint}{sep}{urlencode(params)}", state, verifier


async def exchange_code(
    token_endpoint: str,
    client_id: str,
    client_secret: str | None,
    code: str,
    verifier: str,
    redirect_uri: str,
    resource: str,
    http: httpx.AsyncClient,
) -> dict:
    return await _token_request(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": resource,
        },
        client_secret,
        http,
    )


async def refresh_tokens(
    token_endpoint: str,
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
    resource: str | None,
    http: httpx.AsyncClient,
) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if resource:
        data["resource"] = resource
    return await _token_request(token_endpoint, data, client_secret, http)


async def _token_request(
    token_endpoint: str, data: dict, client_secret: str | None, http: httpx.AsyncClient
) -> dict:
    if client_secret:
        data = {**data, "client_secret": client_secret}
    resp = await http.post(
        token_endpoint,
        data=data,
        headers={"Accept": "application/json"},
        timeout=DISCOVERY_TIMEOUT_S,
    )
    if resp.status_code != 200:
        raise OAuthFlowError(f"Token request failed ({resp.status_code}): {resp.text[:200]}")
    body = resp.json()
    if "access_token" not in body:
        raise OAuthFlowError(f"Token response had no access_token: {str(body)[:200]}")
    return body
