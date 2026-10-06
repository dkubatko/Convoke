"""Image generation and editing for the agent's generate_image / edit_image
tools. POSTs to the OpenAI-compatible /images/generations and /images/edits
endpoints directly — pydantic-ai has no image-output abstraction, same as
transcription in describe.py.

Bytes flow one way: the model's output goes straight to Telegram as an
upload and is never stored (only the prompt-derived description persists,
like every other attachment). Edit sources are downloaded from Telegram by
file_id, sent to the model, and discarded."""

import base64
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.models import Bot, Chat, ConnectedModel
from app.telegram.client import make_bot

log = logging.getLogger("convoke.media")

# Generation at high quality runs well past a minute on big models; the agent
# run's own deadline (900s) is the outer bound.
IMAGE_TIMEOUT_S = 300

# Orientation → size. The three sizes every gpt-image model accepts; None
# omits the parameter so the model picks (and edits keep the source's shape).
IMAGE_SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}


class ImageGenError(RuntimeError):
    """A failed generation/edit, with a message fit to hand back to the agent
    (provider refusals carry their reason)."""


def _headers(provider: ConnectedModel) -> dict[str, str]:
    key = decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else None
    return {"Authorization": f"Bearer {key}"} if key else {}


async def _image_from(resp: httpx.Response, client: httpx.AsyncClient) -> bytes:
    if resp.status_code != 200:
        try:
            detail = resp.json()["error"]["message"]
        except Exception:  # noqa: BLE001 — non-JSON or unexpected error shape
            detail = resp.text
        raise ImageGenError(f"HTTP {resp.status_code}: {detail[:300]}")
    item = resp.json()["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    # Endpoints that return a hosted URL instead (dall-e-style). Provider
    # output, not an agent-supplied URL — safe to fetch.
    if item.get("url"):
        r = await client.get(item["url"])
        r.raise_for_status()
        return r.content
    raise ImageGenError("The image endpoint returned no image.")


async def generate(provider: ConnectedModel, prompt: str, size: str | None = None) -> bytes:
    body: dict = {"model": provider.model_name, "prompt": prompt, "n": 1}
    if size:
        body["size"] = size
    async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S) as client:
        resp = await client.post(
            f"{provider.base_url.rstrip('/')}/images/generations",
            headers=_headers(provider),
            json=body,
        )
        return await _image_from(resp, client)


async def edit(
    provider: ConnectedModel,
    images: list[tuple[bytes, str]],
    prompt: str,
    size: str | None = None,
) -> bytes:
    """`images` is [(bytes, mime)], the first being the main image."""
    data: dict = {"model": provider.model_name, "prompt": prompt, "n": "1"}
    if size:
        data["size"] = size
    files = [
        ("image[]", (f"source{i}.{mime.split('/')[-1]}", raw, mime))
        for i, (raw, mime) in enumerate(images)
    ]
    async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S) as client:
        resp = await client.post(
            f"{provider.base_url.rstrip('/')}/images/edits",
            headers=_headers(provider),
            data=data,
            files=files,
        )
        return await _image_from(resp, client)


async def download_chat_files(
    session: AsyncSession, chat_id: int, file_ids: list[str]
) -> list[bytes]:
    """Fetch Telegram files with the chat's own bot (file_ids are bot-scoped)."""
    bot_row = (
        await session.execute(
            select(Bot).join(Chat, Chat.bot_id == Bot.id).where(Chat.id == chat_id)
        )
    ).scalar_one()
    bot = make_bot(decrypt(bot_row.token_encrypted))
    try:
        out = []
        for file_id in file_ids:
            buf = await bot.download(file_id)
            out.append(buf.read() if buf is not None else b"")
        return out
    finally:
        await bot.session.close()
