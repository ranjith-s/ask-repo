"""Runtime configuration, read from environment variables (12-factor)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://askrepo:askrepo@localhost:5432/askrepo"

    # LLM
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_max_tokens: int = 16000
    # USD per million tokens, used for the per-query cost estimate.
    price_input_per_mtok: float = 4.0
    price_output_per_mtok: float = 20.0
    price_cache_write_per_mtok: float = 5.0
    price_cache_read_per_mtok: float = 0.20

    # Embeddings: "fastembed" (local CPU model) or "hash" (offline, tests only).
    embedding_backend: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # Agent guardrails
    agent_max_steps: int = 8
    tool_result_max_chars: int = 8000
    question_max_chars: int = 2000
    history_max_turns: int = 6

    # Ingestion limits
    clone_timeout_s: int = 120
    max_files: int = 3000
    max_file_bytes: int = 300_000

    log_level: str = "INFO"
    log_json: bool = True
    static_dir: str = "static"


@lru_cache
def get_settings() -> Settings:
    return Settings()
