from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.db import SINGLETON_LOCK_BACKEND, acquire_singleton_lock, get_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.singleton_lock_conn = await acquire_singleton_lock(SINGLETON_LOCK_BACKEND)
    yield
    await app.state.singleton_lock_conn.close()
    await get_engine().dispose()


app = FastAPI(title="Convoke", lifespan=lifespan)
app.include_router(api_router, prefix="/api")
