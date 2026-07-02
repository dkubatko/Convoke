from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONVOKE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://convoke:convoke@localhost:5432/convoke"

    # Operator auth: single shared password for the UI/API.
    operator_password: str
    # Signs session cookies (itsdangerous).
    secret_key: str
    # Encrypts bot tokens / provider credentials at rest (Fernet, urlsafe base64 32 bytes).
    fernet_key: str

    session_max_age_seconds: int = 7 * 24 * 3600
    cookie_secure: bool = False  # set true behind HTTPS

    # Memory / embeddings. Changing the model implies changing the dim, which
    # means a migration + full re-embed — treat as a deployment-time choice.
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384
    embedding_batch_size: int = 64
    # A conversation segment closes after this much silence (or at max size).
    chunk_lull_seconds: int = 30 * 60
    chunk_max_messages: int = 24
    chunk_overlap_messages: int = 4

    imports_dir: str = "/data/imports"

    # Agent context budget in characters (~4 chars per token).
    context_char_budget: int = 24000
    agent_concurrency: int = 4

    # Intent pipeline
    intent_lull_seconds: int = 60
    intent_window_max_messages: int = 30
    intent_min_llm_interval_seconds: int = 120
    intent_state_ttl_hours: int = 36
    confirm_timeout_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
