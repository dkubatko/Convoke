"""Embedding backends.

Local models run in a ProcessPoolExecutor: sentence-transformers is CPU-bound
and would freeze the worker's event loop (and with it every bot's polling).
The model loads once per subprocess.

Two embedder ROLES with separate registries and separate ProcessPools:

- "intent": the prefilter gate compares short message windows against short
  example phrases — a symmetric similarity task. Multilingual PARAPHRASE/STS
  models win here (wide, calibrated similarity range; cross-lingual by
  meaning); retrieval models compress similarity into a narrow band and rank
  topics poorly. Validated on real multilingual traffic.

- "memory": chat-history search embeds a short query against long transcript
  passages — an asymmetric RETRIEVAL task. Here the opposite holds: models
  trained for retrieval (mE5, EmbeddingGemma, BGE-M3) win, and the paraphrase
  family fails badly. Diagnosed live: distiluse's 128-token window silently
  truncated ~80% of every chunk and ranked an exact-match chunk 4,426/4,437.

Doc/query prefixes are data on EmbeddingModelSpec, not code paths — mE5 needs
"query: "/"passage: ", EmbeddingGemma its prompt headers, others none.
"""

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("convoke.embeddings")

_model = None  # per-subprocess singleton
_model_name: str | None = None


# The prefilter's clamp band, global to every model: recall-first calibration
# self-scales to the model's similarity range (see calibrate_threshold), so this
# is only a backstop against degenerate calibration.
GLOBAL_THRESHOLD_FLOOR = 0.15
GLOBAL_THRESHOLD_CEIL = 0.90

ROLES = ("intent", "memory")


@dataclass(frozen=True)
class EmbeddingModelSpec:
    id: str  # HuggingFace model id
    label: str
    dim: int | None  # None → probe by encoding one string
    doc_prefix: str
    query_prefix: str


INTENT_EMBEDDING_REGISTRY: dict[str, EmbeddingModelSpec] = {
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

MEMORY_EMBEDDING_REGISTRY: dict[str, EmbeddingModelSpec] = {
    s.id: s
    for s in (
        EmbeddingModelSpec(
            id="intfloat/multilingual-e5-base",
            label="Multilingual E5 base · 768d · recommended",
            dim=768,
            doc_prefix="passage: ",
            query_prefix="query: ",
        ),
        EmbeddingModelSpec(
            id="intfloat/multilingual-e5-small",
            label="Multilingual E5 small · 384d · lightest",
            dim=384,
            doc_prefix="passage: ",
            query_prefix="query: ",
        ),
        EmbeddingModelSpec(
            id="google/embeddinggemma-300m",
            label="EmbeddingGemma 300M · 768d · gated (needs HF token)",
            dim=768,
            doc_prefix="title: none | text: ",
            query_prefix="task: search result | query: ",
        ),
        EmbeddingModelSpec(
            id="BAAI/bge-m3",
            label="BGE-M3 · 1024d · strongest, heaviest (CPU-slow)",
            dim=1024,
            doc_prefix="",
            query_prefix="",
        ),
    )
}

_REGISTRIES: dict[str, dict[str, EmbeddingModelSpec]] = {
    "intent": INTENT_EMBEDDING_REGISTRY,
    "memory": MEMORY_EMBEDDING_REGISTRY,
}

DEFAULT_MODELS = {
    "intent": "sentence-transformers/distiluse-base-multilingual-cased-v2",
    "memory": "intfloat/multilingual-e5-base",
}


def registry_for(role: str) -> dict[str, EmbeddingModelSpec]:
    return _REGISTRIES[role]


def custom_spec(model_id: str, dim: int | None = None) -> EmbeddingModelSpec:
    """Escape hatch for an operator-supplied HuggingFace id: no prefixes, the
    global clamp band, dim probed at swap time when not given."""
    return EmbeddingModelSpec(
        id=model_id, label=f"{model_id} (custom)", dim=dim,
        doc_prefix="", query_prefix="",
    )


def _get_model(model_name: str):
    global _model, _model_name
    if _model is None or _model_name != model_name:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(model_name, device="cpu")
        _model_name = model_name
    return _model


def _encode(model_name: str, texts: list[str]) -> list[list[float]]:
    model = _get_model(model_name)
    # The encoder silently truncates at max_seq_length — the exact failure
    # that broke memory search once. The chunker budgets against the window,
    # so overflow here means a bug or an over-long single message: warn.
    limit = model.max_seq_length
    lens = [len(ids) for ids in model.tokenizer(texts, truncation=False)["input_ids"]]
    over = sum(1 for n in lens if n > limit)
    if over:
        log.warning(
            "%d/%d passage(s) exceed the %d-token window of %s (max %d) — "
            "their tails will not be searchable",
            over, len(texts), limit, model_name, max(lens),
        )
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()


def _count_tokens(model_name: str, texts: list[str]) -> list[int]:
    model = _get_model(model_name)
    return [len(ids) for ids in model.tokenizer(texts, truncation=False)["input_ids"]]


def _probe_limits(model_name: str) -> tuple[int, int]:
    """(dimension, input window in tokens) — loads the model on first call."""
    model = _get_model(model_name)
    dim = len(model.encode(["dimension probe"], show_progress_bar=False)[0])
    return dim, int(model.max_seq_length)


class Embedder(Protocol):
    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...
    async def count_tokens(self, texts: list[str]) -> list[int]: ...


class LocalEmbedder:
    def __init__(self, spec: EmbeddingModelSpec, batch_size: int = 64) -> None:
        self.spec = spec
        self.batch_size = batch_size
        self._pool = ProcessPoolExecutor(max_workers=1)

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, fn, self.spec.id, *args)
        except BrokenProcessPool:
            # The subprocess died (typically OOM loading a heavy model).
            # A broken pool rejects every later job, so recreate it and
            # retry once rather than wedging all embedding until restart.
            log.warning("embedding subprocess died; recreating pool and retrying once")
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = ProcessPoolExecutor(max_workers=1)
            return await loop.run_in_executor(self._pool, fn, self.spec.id, *args)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(await self._call(_encode, texts[i : i + self.batch_size]))
        return out

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"{self.spec.doc_prefix}{t}" for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([f"{self.spec.query_prefix}{text}"]))[0]

    async def count_tokens(self, texts: list[str]) -> list[int]:
        """Token counts as the model's own tokenizer sees the PASSAGE form of
        each text (doc prefix included) — what the chunker must budget by."""
        out: list[int] = []
        prefixed = [f"{self.spec.doc_prefix}{t}" for t in texts]
        for i in range(0, len(prefixed), self.batch_size):
            out.extend(await self._call(_count_tokens, prefixed[i : i + self.batch_size]))
        return out

    async def probe_limits(self) -> tuple[int, int]:
        """Load the model (downloads on first use); (dim, token window)."""
        return await self._call(_probe_limits)

    async def probe_dim(self) -> int:
        return (await self.probe_limits())[0]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class FakeEmbedder:
    """Deterministic toy embedder for tests: position-independent character
    bigram histogram, so texts sharing words score high regardless of offset."""

    def __init__(self, dim: int = 384, max_tokens: int = 512) -> None:
        self.dim = dim
        self.max_tokens = max_tokens

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

    async def count_tokens(self, texts: list[str]) -> list[int]:
        return [max(1, len(t) // 4) for t in texts]  # ~4 chars/token

    async def probe_limits(self) -> tuple[int, int]:
        return self.dim, self.max_tokens
