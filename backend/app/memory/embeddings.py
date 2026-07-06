"""Embedding backends.

Local models run in a ProcessPoolExecutor: sentence-transformers is CPU-bound
and would freeze the worker's event loop (and with it every bot's polling).
The model loads once per subprocess.

Prefix schemes differ per model family (e5's "passage: "/"query: ",
EmbeddingGemma's prompt strings, Qwen's query instruction, BGE's nothing) —
they're data on EmbeddingModelSpec, never code paths. The doc/query asymmetry
is also what makes the intent prefilter work (trigger examples vs windows).
"""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Protocol

_model = None  # per-subprocess singleton
_model_name: str | None = None


@dataclass(frozen=True)
class EmbeddingModelSpec:
    id: str  # HuggingFace model id
    label: str
    dim: int | None  # None → probe by encoding one string
    doc_prefix: str
    query_prefix: str
    # Calibration clamp band for prefilter thresholds — similarity scales are
    # model-specific (e5 compresses everything into ~0.80–0.95).
    threshold_floor: float
    threshold_ceil: float


EMBEDDING_REGISTRY: dict[str, EmbeddingModelSpec] = {
    s.id: s
    for s in (
        EmbeddingModelSpec(
            id="intfloat/multilingual-e5-small",
            label="Multilingual E5 small (default) · 384d · fastest",
            dim=384,
            doc_prefix="passage: ",
            query_prefix="query: ",
            threshold_floor=0.70,
            threshold_ceil=0.88,
        ),
        EmbeddingModelSpec(
            id="ibm-granite/granite-embedding-278m-multilingual",
            label="Granite Embedding 278m · 768d · open",
            dim=768,
            doc_prefix="",
            query_prefix="",
            threshold_floor=0.45,
            threshold_ceil=0.90,
        ),
        EmbeddingModelSpec(
            id="google/embeddinggemma-300m",
            label="EmbeddingGemma 300m · 768d · gated: accept the HF license + set HF_TOKEN",
            dim=768,
            doc_prefix="title: none | text: ",
            query_prefix="task: search result | query: ",
            threshold_floor=0.55,
            threshold_ceil=0.90,
        ),
        EmbeddingModelSpec(
            id="BAAI/bge-m3",
            label="BGE-M3 · 1024d · strongest, heaviest",
            dim=1024,
            doc_prefix="",
            query_prefix="",
            threshold_floor=0.45,
            threshold_ceil=0.90,
        ),
        EmbeddingModelSpec(
            id="Qwen/Qwen3-Embedding-0.6B",
            label="Qwen3 Embedding 0.6B · 1024d",
            dim=1024,
            doc_prefix="",
            query_prefix=(
                "Instruct: Given a web search query, retrieve relevant passages "
                "that answer the query\nQuery: "
            ),
            threshold_floor=0.45,
            threshold_ceil=0.90,
        ),
    )
}


def custom_spec(model_id: str, dim: int | None = None) -> EmbeddingModelSpec:
    """Escape hatch for an operator-supplied HuggingFace id: no prefixes,
    generic clamp band, dim probed at swap time when not given."""
    return EmbeddingModelSpec(
        id=model_id, label=f"{model_id} (custom)", dim=dim,
        doc_prefix="", query_prefix="",
        threshold_floor=0.45, threshold_ceil=0.92,
    )


def _encode(model_name: str, texts: list[str]) -> list[list[float]]:
    global _model, _model_name
    if _model is None or _model_name != model_name:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(model_name, device="cpu")
        _model_name = model_name
    return _model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()


class Embedder(Protocol):
    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


class LocalEmbedder:
    def __init__(self, spec: EmbeddingModelSpec, batch_size: int = 64) -> None:
        self.spec = spec
        self.batch_size = batch_size
        self._pool = ProcessPoolExecutor(max_workers=1)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            out.extend(await loop.run_in_executor(self._pool, _encode, self.spec.id, batch))
        return out

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"{self.spec.doc_prefix}{t}" for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([f"{self.spec.query_prefix}{text}"]))[0]

    async def probe_dim(self) -> int:
        """Load the model (downloads on first use) and measure its dimension."""
        return len((await self._embed(["dimension probe"]))[0])

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class FakeEmbedder:
    """Deterministic toy embedder for tests: position-independent character
    bigram histogram, so texts sharing words score high regardless of offset."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        t = text.lower()
        for a, b in zip(t, t[1:]):
            v[(ord(a) * 31 + ord(b)) % self.dim] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._one(text)
