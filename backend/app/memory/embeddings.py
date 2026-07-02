"""Embedding backends.

The local default (multilingual-e5-small) runs in a ProcessPoolExecutor:
sentence-transformers is CPU-bound and would freeze the worker's event loop
(and with it every bot's polling). The model loads once per subprocess.

e5 models are asymmetric: documents embed with a "passage: " prefix, queries
with "query: ". This asymmetry is also what makes the intent prefilter work
(trigger examples as queries vs chat windows as passages).
"""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from typing import Protocol

_model = None  # per-subprocess singleton
_model_name: str | None = None


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
    def __init__(self, model_name: str, batch_size: int = 64) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._pool = ProcessPoolExecutor(max_workers=1)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        loop = asyncio.get_running_loop()
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            out.extend(await loop.run_in_executor(self._pool, _encode, self.model_name, batch))
        return out

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"passage: {t}" for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([f"query: {text}"]))[0]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class FakeEmbedder:
    """Deterministic toy embedder for tests: bag-of-character-buckets."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for i, ch in enumerate(text.lower()):
            v[(ord(ch) * 31 + i) % self.dim] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._one(text)
