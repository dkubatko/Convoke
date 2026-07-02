import asyncio

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.models import ModelProvider

TEST_TIMEOUT_S = 20


class ProviderNotConfigured(RuntimeError):
    def __init__(self, role: str):
        super().__init__(f"No model provider configured for role '{role}'")
        self.role = role


async def get_provider(session: AsyncSession, role: str) -> ModelProvider:
    provider = (
        await session.execute(select(ModelProvider).where(ModelProvider.role == role))
    ).scalar_one_or_none()
    if provider is None:
        raise ProviderNotConfigured(role)
    return provider


def build_model(provider: ModelProvider) -> OpenAIChatModel:
    """Any OpenAI-compatible endpoint: Ollama, LM Studio, OpenRouter, OpenAI…"""
    api_key = decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else "unused"
    return OpenAIChatModel(
        provider.model_name,
        provider=OpenAIProvider(base_url=provider.base_url, api_key=api_key),
    )


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
