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
    # Where the operator's browser reaches Convoke — OAuth redirect URIs are
    # built from this. Override when hosting beyond localhost.
    public_url: str = "http://localhost:8080"

    # Memory / embeddings. The models are operator-configurable at runtime
    # (embedding_state table, one row per role, Models page); migrations seed
    # the defaults. Only the batch size is env config.
    embedding_batch_size: int = 64
    # A conversation segment closes after this much silence, at the token
    # budget, or at max size. The token budget is the retrieval sweet spot AND
    # the truncation guard — it is always clamped to the memory model's input
    # window. Operator-tunable on the Models page; applies to new chunks, a
    # rebuild re-cuts history.
    chunk_target_tokens: int = 512
    chunk_lull_seconds: int = 30 * 60
    chunk_max_messages: int = 24
    chunk_overlap_messages: int = 4
    # 1 (default): chunk vectors are computed from human lines only — bot
    # replies paraphrase queries and summarize facts, so scoring them lets the
    # bot's own chatter outrank the answers (measured live). 0: score bot
    # lines too. Covers ALL bot senders: the connected bot (live + imported
    # history) and members flagged is_bot. Either way bot lines stay in chunk
    # text, lexical search, and direct reads. Applies on the next (re-)embed;
    # Rebuild index for history.
    memory_ignore_bot_messages: int = 1

    imports_dir: str = "/data/imports"

    # Media understanding. Bytes are downloaded transiently for description
    # and deleted — only text (descriptions/transcripts) persists.
    media_max_download_bytes: int = 20 * 2**20  # Bot API getFile ceiling
    media_describe_concurrency: int = 4  # parallel describe/transcribe calls per tick
    media_description_max_chars: int = 400  # keep chunk vectors text-dominated
    # Hold an intent window open this long while media in it is still being
    # described, so the classifier sees descriptions, not placeholders.
    intent_media_grace_seconds: int = 120
    video_sample_frames: int = 3

    # Agent context budget in characters (~4 chars per token).
    context_char_budget: int = 24000
    agent_concurrency: int = 4

    # Intent pipeline. Timing defaults tuned for quicker reaction while keeping
    # a settled-burst window and a model-cost cap.
    intent_lull_seconds: int = 30
    intent_window_max_messages: int = 30
    # Messages before the evaluation cursor shown to the classifier as context.
    intent_context_messages: int = 8
    # Circuit-breaker floor between successful classifier calls per
    # (workflow, thread) — the lull is the real rate limiter; this only stops
    # a pathological burst-close loop from hammering the model.
    intent_min_llm_interval_seconds: int = 15
    # Concurrent classifier calls across all (workflow, chat, thread) jobs.
    intent_classifier_concurrency: int = 4
    # Episode lifecycle. A `candidate` (classifier said "plausible, not enough
    # info") expires fast; `tracking` lives while the negotiation does.
    intent_candidate_ttl_minutes: int = 20
    intent_candidate_unrelated_k: int = 3
    intent_tracking_idle_hours: int = 12
    intent_episode_max_age_days: int = 7
    # Concurrent pre-fire episodes per (workflow, thread). 1 until the small
    # model's attribution quality is validated live; raise to ~3 after.
    intent_max_open_episodes: int = 1
    # Graduated slot decay: after the grace period of no attributed activity,
    # each slot's effective confidence is multiplied by (pct/100) per hour —
    # computed lazily, never written back.
    intent_decay_grace_hours: int = 6
    intent_decay_per_hour_pct: int = 85
    # How often the detector loop wakes to look for windows to evaluate. One
    # global loop processes every chat — not a per-chat knob.
    intent_sweep_interval_seconds: int = 5
    # Positive example phrases the strong model generates per workflow to
    # calibrate the prefilter (negatives scale with it).
    intent_example_count: int = 18
    # Prefilter permissiveness, 1 (strictest) … 5 (most permissive). A coarse
    # recall/noise knob: higher places the embedding threshold lower, so more
    # windows reach the classifier (recall up, cheap-model noise up). Recall-
    # first default leans permissive. Changing it recalibrates every intent
    # workflow's threshold from its stored example vectors (no re-embed).
    intent_prefilter_permissiveness: int = 4
    confirm_timeout_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
