from functools import lru_cache

from app.core.config import get_settings
from app.memory.embeddings import LocalEmbedder


@lru_cache
def get_embedder() -> LocalEmbedder:
    s = get_settings()
    return LocalEmbedder(s.embedding_model, batch_size=s.embedding_batch_size)
