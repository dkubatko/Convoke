import os

os.environ.setdefault("CONVOKE_OPERATOR_PASSWORD", "test-password")
os.environ.setdefault("CONVOKE_SECRET_KEY", "test-secret-key")
os.environ.setdefault("CONVOKE_FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

import httpx
import pytest

from app.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_login_rejects_wrong_password(client):
    resp = await client.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 401


async def test_login_sets_session_and_me_works(client):
    resp = await client.post("/api/auth/login", json={"password": "test-password"})
    assert resp.status_code == 200
    assert "convoke_session" in resp.cookies

    me = await client.get("/api/auth/me", cookies={"convoke_session": resp.cookies["convoke_session"]})
    assert me.status_code == 200
    assert me.json() == {"role": "operator"}


async def test_me_requires_session(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_rejects_forged_session(client):
    resp = await client.get("/api/auth/me", cookies={"convoke_session": "forged.token.value"})
    assert resp.status_code == 401
