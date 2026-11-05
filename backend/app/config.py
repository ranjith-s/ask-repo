"""Runtime configuration, read from environment variables (12-factor)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo-root .env (shared with Docker Compose) is read when running from backend/;
# a backend/.env, if present, overrides it. Real environment variables win over both.
ENV_FILES = ("../.env", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, extra="ignore")

    database_url: str = "postgresql://askrepo:askrepo@localhost:5432/askrepo"

    # LLM. Provider: "anthropic" | "gemini" | "ollama". The model defaults per provider.
    llm_provider: str = "anthropic"
    llm_model: str | None = None
    llm_effort: str = "medium"  # Claude only: low | medium | high
    llm_max_tokens: int = 16000
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_num_ctx: int = 32768  # Ollama's default context is too small for tool results
    # USD per million tokens. Leave unset to use the built-in table in llm/pricing.py.
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None
    price_cache_write_per_mtok: float | None = None
    price_cache_read_per_mtok: float | None = None

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

    @property
    def model(self) -> str:
        from app.agent.llm.pricing import DEFAULT_MODELS

        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider, "")

    def llm_problem(self) -> str | None:
        """Why the configured LLM can't be used, or None if it looks ready."""
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            return "ANTHROPIC_API_KEY is not set"
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            return "GEMINI_API_KEY is not set"
        if self.llm_provider not in ("anthropic", "gemini", "ollama"):
            return f"Unknown LLM_PROVIDER: {self.llm_provider}"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
