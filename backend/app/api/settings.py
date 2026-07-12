from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.runtime_settings import (
    TUNABLES,
    check_confidence_bars,
    default_for,
    load_chat_overrides,
    load_overrides,
    set_chat_override,
    set_override,
)
from app.core.security import require_operator
from app.intent.examples import recalibrate_intent_thresholds
from app.models import Chat

router = APIRouter(dependencies=[Depends(require_operator)])


class SettingOut(BaseModel):
    key: str
    label: str
    description: str
    unit: str
    minimum: int
    maximum: int
    value: int  # effective (override or default)
    default: int
    overridden: bool
    group: str  # topic header the setting is listed under
    # Present for step-labelled knobs: one label per integer minimum..maximum,
    # rendered as a named N-stop control instead of a numeric slider.
    step_labels: list[str] | None = None


class SettingUpdate(BaseModel):
    key: str
    value: int


# ---------- global (edited on Models / Workflows) ----------

@router.get("/settings", response_model=list[SettingOut])
async def list_settings(
    page: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[SettingOut]:
    overrides = await load_overrides(session)
    out: list[SettingOut] = []
    for t in TUNABLES:
        if t.scope != "global" or (page and t.page != page):
            continue
        default = default_for(t.key)
        out.append(
            SettingOut(
                key=t.key, label=t.label, description=t.description, unit=t.unit,
                minimum=t.minimum, maximum=t.maximum,
                value=overrides.get(t.key, default), default=default,
                overridden=t.key in overrides, group=t.group,
                step_labels=list(t.step_labels) if t.step_labels else None,
            )
        )
    return out


@router.put("/settings", response_model=list[SettingOut])
async def update_settings(
    updates: list[SettingUpdate], session: AsyncSession = Depends(get_session)
) -> list[SettingOut]:
    try:
        for u in updates:
            await set_override(session, u.key, u.value)
        await check_confidence_bars(session)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    # The permissiveness knob has an effect only through the workflow thresholds
    # it calibrates — re-derive them from the stored example vectors now (cheap:
    # no model calls, no re-embed) so the change lands immediately, whether the
    # override was set or cleared back to the default.
    if any(u.key == "intent_prefilter_permissiveness" for u in updates):
        effective = (await load_overrides(session)).get(
            "intent_prefilter_permissiveness", default_for("intent_prefilter_permissiveness")
        )
        await recalibrate_intent_thresholds(session, effective)
    await session.commit()
    return await list_settings(session=session)


# ---------- per-chat (edited on a chat's Settings tab) ----------

@router.get("/chats/{chat_id}/settings", response_model=list[SettingOut])
async def list_chat_settings(
    chat_id: int, session: AsyncSession = Depends(get_session)
) -> list[SettingOut]:
    if await session.get(Chat, chat_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    overrides = await load_chat_overrides(session, chat_id)
    return [
        SettingOut(
            key=t.key, label=t.label, description=t.description, unit=t.unit,
            minimum=t.minimum, maximum=t.maximum,
            value=overrides.get(t.key, default_for(t.key)), default=default_for(t.key),
            overridden=t.key in overrides, group=t.group,
        )
        for t in TUNABLES
        if t.scope == "chat"
    ]


@router.put("/chats/{chat_id}/settings", response_model=list[SettingOut])
async def update_chat_settings(
    chat_id: int, updates: list[SettingUpdate], session: AsyncSession = Depends(get_session)
) -> list[SettingOut]:
    if await session.get(Chat, chat_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found")
    try:
        for u in updates:
            await set_chat_override(session, chat_id, u.key, u.value)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    await session.commit()
    return await list_chat_settings(chat_id, session=session)
