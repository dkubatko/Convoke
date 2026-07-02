from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt
from app.core.db import get_session
from app.core.security import require_operator
from app.models import ModelProvider
from app.models.agents import PROVIDER_ROLES

router = APIRouter(dependencies=[Depends(require_operator)])


class ProviderIn(BaseModel):
    base_url: str
    model_name: str
    # None = keep existing key; "" = clear (endpoint needs no key)
    api_key: str | None = None


class ProviderOut(BaseModel):
    role: str
    base_url: str
    model_name: str
    has_api_key: bool
    updated_at: datetime


def _out(p: ModelProvider) -> ProviderOut:
    return ProviderOut(
        role=p.role,
        base_url=p.base_url,
        model_name=p.model_name,
        has_api_key=p.api_key_encrypted is not None,
        updated_at=p.updated_at,
    )


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(session: AsyncSession = Depends(get_session)) -> list[ProviderOut]:
    rows = (await session.execute(select(ModelProvider).order_by(ModelProvider.role))).scalars()
    return [_out(p) for p in rows]


@router.put("/providers/{role}", response_model=ProviderOut)
async def upsert_provider(
    role: str, body: ProviderIn, session: AsyncSession = Depends(get_session)
) -> ProviderOut:
    if role not in PROVIDER_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown role '{role}'")
    provider = (
        await session.execute(select(ModelProvider).where(ModelProvider.role == role))
    ).scalar_one_or_none()
    if provider is None:
        provider = ModelProvider(role=role, base_url="", model_name="")
        session.add(provider)
    provider.base_url = body.base_url.rstrip("/")
    provider.model_name = body.model_name
    if body.api_key is not None:
        provider.api_key_encrypted = encrypt(body.api_key) if body.api_key else None
    await session.commit()
    return _out(provider)


@router.delete("/providers/{role}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(role: str, session: AsyncSession = Depends(get_session)) -> None:
    provider = (
        await session.execute(select(ModelProvider).where(ModelProvider.role == role))
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found")
    await session.delete(provider)
    await session.commit()
