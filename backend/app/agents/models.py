import asyncio

import httpx
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.media.assets import TEST_PNG, TEST_WAV
from app.models import ConnectedModel, ModelRoleAssignment

TEST_TIMEOUT_S = 20


class ProviderNotConfigured(RuntimeError):
    def __init__(self, role: str):
        super().__init__(f"No model assigned to role '{role}'")
        self.role = role


def reasoning_settings(effort: str | None) -> dict:
    """model_settings fragment for a reasoning level; {} for Default (the
    parameter is omitted, never sent as 'none')."""
    return {"openai_reasoning_effort": effort} if effort else {}


async def get_role_reasoning(session: AsyncSession, role: str) -> str | None:
    a = await session.get(ModelRoleAssignment, role)
    return a.reasoning_effort if a is not None else None


async def probe_reasoning(provider: ConnectedModel, effort: str) -> tuple[bool, str]:
    """One micro-call with the requested effort — there is no discovery API
    for supported levels anywhere in the OpenAI-compatible ecosystem, so the
    only truth is asking. max_tokens is roomy: reasoning burns tokens before
    the first output token, and a length-starved probe would false-negative."""
    agent = Agent(
        build_model(provider),
        model_settings={"max_tokens": 1024, **reasoning_settings(effort)},
    )
    try:
        await asyncio.wait_for(agent.run("Reply with exactly: OK"), TEST_TIMEOUT_S)
    except TimeoutError:
        return False, f"No response within {TEST_TIMEOUT_S}s probing reasoning_effort='{effort}'."
    except ModelHTTPError as e:
        return False, f"The endpoint rejected reasoning_effort='{effort}': {str(e)[:200]}"
    except Exception as e:  # noqa: BLE001 — surface as a failed probe
        return False, f"{type(e).__name__}: {str(e)[:200]}"
    return True, f"reasoning_effort='{effort}' accepted."


async def get_provider(session: AsyncSession, role: str) -> ConnectedModel:
    """Resolve a role to its assigned connected model."""
    model = (
        await session.execute(
            select(ConnectedModel)
            .join(ModelRoleAssignment, ModelRoleAssignment.model_id == ConnectedModel.id)
            .where(ModelRoleAssignment.role == role)
        )
    ).scalar_one_or_none()
    if model is None:
        raise ProviderNotConfigured(role)
    return model


# Each OpenAIProvider owns an AsyncOpenAI/httpx client. The intent sweeper
# builds one per (workflow × window) evaluation, so without caching the
# long-running worker leaks connection pools forever. Key on the config that
# defines the client; invalidate implicitly when any field changes.
_model_cache: dict[tuple[str, str, str], OpenAIChatModel] = {}


def build_model(provider: ConnectedModel) -> OpenAIChatModel:
    """Any OpenAI-compatible endpoint: Ollama, LM Studio, OpenRouter, OpenAI…"""
    api_key = decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else "unused"
    key = (provider.base_url, provider.model_name, api_key)
    cached = _model_cache.get(key)
    if cached is None:
        cached = OpenAIChatModel(
            provider.model_name,
            provider=OpenAIProvider(base_url=provider.base_url, api_key=api_key),
        )
        # Bound the cache: a handful of role/config combos in practice.
        if len(_model_cache) > 16:
            _model_cache.clear()
        _model_cache[key] = cached
    return cached


def evict_model(provider: ConnectedModel) -> None:
    """Drop the cached client for this endpoint after a failed model call.
    A poisoned connection pool (seen live: every vision call failing with
    'Connection error' while other endpoints on the same host worked)
    otherwise survives every retry until the worker restarts. The evicted
    client is left to the GC — a rare, bounded leak, same trade-off as the
    cache-size clear above."""
    api_key = decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else "unused"
    _model_cache.pop((provider.base_url, provider.model_name, api_key), None)


async def probe_endpoint(base_url: str, model_name: str, api_key: str | None) -> tuple[bool, str]:
    """Fire one tiny completion at an endpoint to prove it's real before the
    operator saves it. Returns (ok, human-readable detail)."""
    model = OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=base_url, api_key=api_key or "unused"),
    )
    agent = Agent(model, model_settings={"max_tokens": 16})
    try:
        result = await asyncio.wait_for(agent.run("Reply with exactly: OK"), TEST_TIMEOUT_S)
    except TimeoutError:
        return False, f"No response within {TEST_TIMEOUT_S}s — endpoint reachable but not answering?"
    except ModelHTTPError as e:
        detail = str(e)
        if "401" in detail or "unauthorized" in detail.lower():
            return False, "The endpoint rejected the API key (401)."
        if "404" in detail or "not found" in detail.lower():
            return False, f"The endpoint doesn't know the model '{model_name}' (404)."
        return False, f"The endpoint returned an error: {detail[:200]}"
    except Exception as e:  # noqa: BLE001 — surface anything else as text
        # Connection failures arrive wrapped (ModelAPIError -> APIConnectionError
        # -> httpx.ConnectError); walk the cause chain to recognize them.
        unreachable = False
        cause: BaseException | None = e
        for _ in range(4):
            if cause is None:
                break
            if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)) or type(
                cause
            ).__name__ == "APIConnectionError":
                unreachable = True
                break
            cause = cause.__cause__
        unreachable = unreachable or "connection error" in str(e).lower()
        if unreachable:
            return False, (
                f"Couldn't reach {base_url} — check the URL "
                "(from Docker, host services are at http://host.docker.internal)."
            )
        return False, f"{type(e).__name__}: {str(e)[:200]}"
    reply = (result.output or "").strip()
    return True, f"Model replied: {reply[:60] or '(empty)'}"


async def probe_vision(base_url: str, model_name: str, api_key: str | None) -> tuple[bool, str]:
    """Send a tiny in-code PNG; a model that accepts image parts is
    vision-capable (we assert HTTP acceptance, not that it *sees* well)."""
    model = OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(base_url=base_url, api_key=api_key or "unused"),
    )
    agent = Agent(model, model_settings={"max_tokens": 16})
    try:
        result = await asyncio.wait_for(
            agent.run(
                [
                    "What color is this square? Reply with one word.",
                    BinaryContent(TEST_PNG, media_type="image/png"),
                ]
            ),
            TEST_TIMEOUT_S,
        )
    except TimeoutError:
        return False, f"No response within {TEST_TIMEOUT_S}s."
    except Exception as e:  # noqa: BLE001 — a rejection of image parts lands here
        return False, f"Endpoint rejected image input: {str(e)[:150]}"
    reply = (result.output or "").strip()
    return True, f"Model replied: {reply[:60] or '(empty)'}"


async def probe_transcription(base_url: str, model_name: str, api_key: str | None) -> tuple[bool, str]:
    """POST a 0.2s silent WAV to the OpenAI-compatible transcription endpoint
    (OpenAI, faster-whisper-server, speaches…)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/audio/transcriptions",
                headers=headers,
                data={"model": model_name, "response_format": "json"},
                files={"file": ("probe.wav", TEST_WAV, "audio/wav")},
            )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False, f"Couldn't reach {base_url}."
    except Exception as e:  # noqa: BLE001 — surface as a failed probe
        return False, f"{type(e).__name__}: {str(e)[:150]}"
    if resp.status_code == 200:
        return True, "Transcription endpoint responded."
    return False, f"HTTP {resp.status_code}: {resp.text[:150]}"


async def probe_capabilities(
    base_url: str, model_name: str, api_key: str | None
) -> dict[str, tuple[bool, str]]:
    """Run the chat, vision, and transcription probes concurrently. A model is
    worth saving if any passes (a whisper server fails the chat probe by
    design). Video capability is operator-declared — probing video content
    parts is unreliable across gateways."""
    chat, vision, transcription = await asyncio.gather(
        probe_endpoint(base_url, model_name, api_key),
        probe_vision(base_url, model_name, api_key),
        probe_transcription(base_url, model_name, api_key),
    )
    return {"chat": chat, "vision": vision, "transcription": transcription}
