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


@lru_cache
def get_settings() -> Settings:
    return Settings()
