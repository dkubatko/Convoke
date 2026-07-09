"""Process-wide embedder access, one handle per role.

get_embedder(role) returns a stable EmbedderHandle that every loop and API
endpoint holds; the re-embed job (and ensure_embedder, on the API side) swap
the inner LocalEmbedder live when the operator changes that role's model —
zero call-site churn. The two roles run separate ProcessPools so the intent
gate and memory retrieval each keep their own resident model (no reload
thrash when both are active)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.memory.embeddings import (
    DEFAULT_MODELS,
    ROLES,
    EmbeddingModelSpec,
    LocalEmbedder,
    custom_spec,
    registry_for,
)
from app.models import EmbeddingState

_handles: dict[str, "EmbedderHandle"] = {}


def spec_for(role: str, model_id: str, dim: int | None = None) -> EmbeddingModelSpec:
    return registry_for(role).get(model_id) or custom_spec(model_id, dim)


def spec_from_state(state: EmbeddingState) -> EmbeddingModelSpec:
    """Reconstruct the spec from persisted fields — a custom HF model must
    survive restarts without a registry entry."""
    registry = registry_for(state.role)
    return EmbeddingModelSpec(
        id=state.model_id,
        label=registry[state.model_id].label
        if state.model_id in registry
        else f"{state.model_id} (custom)",
        dim=state.dim or None,
        doc_prefix=state.doc_prefix,
        query_prefix=state.query_prefix,
    )


async def embedding_state_for(session: AsyncSession, role: str) -> EmbeddingState | None:
    """The role's state row — the single lookup every caller shares."""
    return (
        await session.execute(select(EmbeddingState).where(EmbeddingState.role == role))
    ).scalar_one_or_none()


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

    async def count_tokens(self, texts: list[str]) -> list[int]:
        return await self._inner.count_tokens(texts)

    async def probe_limits(self) -> tuple[int, int]:
        return await self._inner.probe_limits()

    def replace(self, new_inner: LocalEmbedder) -> None:
        old, self._inner = self._inner, new_inner
        old.shutdown()


def get_embedder(role: str) -> EmbedderHandle:
    """Bootstraps with the registry default; ensure_embedder aligns it with
    embedding_state (the source of truth) once a session is available."""
    if role not in ROLES:
        raise ValueError(f"unknown embedder role {role!r}")
    if role not in _handles:
        _handles[role] = EmbedderHandle(
            LocalEmbedder(
                registry_for(role)[DEFAULT_MODELS[role]],
                batch_size=get_settings().embedding_batch_size,
            )
        )
    return _handles[role]


async def ensure_embedder(session: AsyncSession, role: str) -> EmbedderHandle:
    """The handle must follow embedding_state across processes: the worker's
    re-embed job swaps its own handle directly, but the API process (search,
    example generation) learns about a swap here — one cheap indexed read."""
    handle = get_embedder(role)
    state = await embedding_state_for(session, role)
    if state is not None and state.model_id != handle.spec.id:
        batch = get_settings().embedding_batch_size
        handle.replace(LocalEmbedder(spec_from_state(state), batch_size=batch))
    return handle


async def backfill_window(session: AsyncSession, role: str) -> None:
    """Probe and persist the model's input window for rows that predate
    max_tokens (migration seeds 0 = unknown). Until this runs, the chunker
    can't clamp its token budget to the encoder's real window — for the
    legacy distiluse-as-memory config that window is 128, the silent
    truncation that broke retrieval. Worker startup calls this per role."""
    state = await embedding_state_for(session, role)
    if state is None or state.status != "ready" or state.max_tokens:
        return
    _, window = await get_embedder(role).probe_limits()
    state.max_tokens = window
    await session.commit()
