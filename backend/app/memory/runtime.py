"""Process-wide embedder access.

get_embedder() returns a stable EmbedderHandle that every loop and API
endpoint holds; the re-embed job (and ensure_embedder, on the API side) swap
the inner LocalEmbedder live when the operator changes the model — zero
call-site churn."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.memory.embeddings import (
    EMBEDDING_REGISTRY,
    EmbeddingModelSpec,
    LocalEmbedder,
    custom_spec,
)
from app.models import EmbeddingState

_handle: "EmbedderHandle | None" = None


def spec_for(model_id: str, dim: int | None = None) -> EmbeddingModelSpec:
    return EMBEDDING_REGISTRY.get(model_id) or custom_spec(model_id, dim)


def spec_from_state(state: EmbeddingState) -> EmbeddingModelSpec:
    """Reconstruct the spec from persisted fields — a custom HF model must
    survive restarts without a registry entry."""
    return EmbeddingModelSpec(
        id=state.model_id,
        label=EMBEDDING_REGISTRY[state.model_id].label
        if state.model_id in EMBEDDING_REGISTRY
        else f"{state.model_id} (custom)",
        dim=state.dim or None,
        doc_prefix=state.doc_prefix,
        query_prefix=state.query_prefix,
    )


class EmbedderHandle:
    """Implements the Embedder protocol by delegation; replace() swaps the
    inner model live and shuts the old pool down."""

    def __init__(self, inner: LocalEmbedder) -> None:
        self._inner = inner

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._inner.spec

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return await self._inner.embed_passages(texts)

    async def embed_query(self, text: str) -> list[float]:
        return await self._inner.embed_query(text)

    def replace(self, new_inner: LocalEmbedder) -> None:
        old, self._inner = self._inner, new_inner
        old.shutdown()


DEFAULT_EMBEDDING_MODEL = "sentence-transformers/distiluse-base-multilingual-cased-v2"


def get_embedder() -> EmbedderHandle:
    """Bootstraps with the registry default; ensure_embedder aligns it with
    embedding_state (the source of truth) once a session is available."""
    global _handle
    if _handle is None:
        _handle = EmbedderHandle(
            LocalEmbedder(
                EMBEDDING_REGISTRY[DEFAULT_EMBEDDING_MODEL],
                batch_size=get_settings().embedding_batch_size,
            )
        )
    return _handle


async def ensure_embedder(session: AsyncSession) -> EmbedderHandle:
    """The handle must follow embedding_state across processes: the worker's
    re-embed job swaps its own handle directly, but the API process (search,
    example generation) learns about a swap here — one cheap PK read."""
    handle = get_embedder()
    state = await session.get(EmbeddingState, 1)
    if state is not None and state.model_id != handle.spec.id:
        batch = get_settings().embedding_batch_size
        handle.replace(LocalEmbedder(spec_from_state(state), batch_size=batch))
    return handle
