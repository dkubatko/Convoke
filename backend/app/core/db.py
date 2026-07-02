from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# Advisory-lock keys: the backend and the worker must each run as singletons,
# because getUpdates polling and offset acks tolerate exactly one consumer per bot.
SINGLETON_LOCK_BACKEND = 0xC0400001
SINGLETON_LOCK_WORKER = 0xC0400002


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


class SingletonLockError(RuntimeError):
    pass


async def acquire_singleton_lock(key: int) -> object:
    """Take a session-scoped pg advisory lock on a dedicated connection.

    Returns the connection, which must stay open for the process lifetime —
    closing it releases the lock. Raises if another instance already holds it,
    so an accidental second replica fails loudly instead of double-polling bots.
    """
    from sqlalchemy import text

    conn = await get_engine().connect()
    got = (await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})).scalar()
    if not got:
        await conn.close()
        raise SingletonLockError(
            f"Another instance already holds singleton lock {key:#x}; "
            "Convoke backend/worker must run as a single replica."
        )
    return conn
