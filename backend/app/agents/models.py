from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.models import ModelProvider


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
