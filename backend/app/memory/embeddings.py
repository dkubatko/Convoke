"""Embedding backends.

Local models run in a ProcessPoolExecutor: sentence-transformers is CPU-bound
and would freeze the worker's event loop (and with it every bot's polling).
The model loads once per subprocess.

Doc/query prefixes are data on EmbeddingModelSpec, not code paths — the shipped
paraphrase models need none, but a custom model can carry its own.
"""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Protocol

_model = None  # per-subprocess singleton
_model_name: str | None = None


# The prefilter's clamp band, global to every model: recall-first calibration
# self-scales to the model's similarity range (see calibrate_threshold), so this
# is only a backstop against degenerate calibration.
GLOBAL_THRESHOLD_FLOOR = 0.15
GLOBAL_THRESHOLD_CEIL = 0.90


@dataclass(frozen=True)
class EmbeddingModelSpec:
    id: str  # HuggingFace model id
    label: str
    dim: int | None  # None → probe by encoding one string
    doc_prefix: str
    query_prefix: str


# Multilingual PARAPHRASE/STS models — not retrieval models. Retrieval embedders
# (e5, BGE, Granite) compress similarity into a narrow high band and rank
# language/topic poorly for a cross-lingual intent gate; paraphrase models keep
# a wide, calibrated range and align languages by meaning, so one English
# example set covers every chat language. Validated on real multilingual
# traffic: distiluse separated on-/off-topic cleanly, mpnet/minilm weaker.
EMBEDDING_REGISTRY: dict[str, EmbeddingModelSpec] = {
    s.id: s
    for s in (
        EmbeddingModelSpec(
            id="sentence-transformers/distiluse-base-multilingual-cased-v2",
            label="Distiluse multilingual · 512d · recommended",
            dim=512,
            doc_prefix="",
            query_prefix="",
        ),
        EmbeddingModelSpec(
            id="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
            label="Paraphrase multilingual MPNet · 768d · weaker",
            dim=768,
            doc_prefix="",
            query_prefix="",
        ),
        EmbeddingModelSpec(
            id="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            label="Paraphrase multilingual MiniLM · 384d · weaker, lightest",
            dim=384,
            doc_prefix="",
            query_prefix="",
        ),
    )
}


def custom_spec(model_id: str, dim: int | None = None) -> EmbeddingModelSpec:
    """Escape hatch for an operator-supplied HuggingFace id: no prefixes, the
    global clamp band, dim probed at swap time when not given."""
    return EmbeddingModelSpec(
        id=model_id, label=f"{model_id} (custom)", dim=dim,
        doc_prefix="", query_prefix="",
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
