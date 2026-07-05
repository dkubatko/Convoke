import os

os.environ.setdefault("CONVOKE_OPERATOR_PASSWORD", "test-password")
os.environ.setdefault("CONVOKE_SECRET_KEY", "test-secret-key")
os.environ.setdefault("CONVOKE_FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base


@pytest.fixture
async def db_sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def client(db_sessionmaker):
    """API client against the app with the test DB and operator auth bypassed."""
    from app.core.db import get_session
    from app.core.security import require_operator
    from app.main import app

    async def _session():
        async with db_sessionmaker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[require_operator] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
