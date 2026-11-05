"""LLM providers behind one interface. Pick one with LLM_PROVIDER."""

from app.agent.llm.base import (
    LLM,
    AssistantMessage,
    LLMRefusal,
    LLMUnavailable,
    Message,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    ToolSpec,
    TurnResult,
    Usage,
    UserMessage,
)
from app.agent.llm.pricing import DEFAULT_MODELS, Price, price_for
from app.config import Settings

__all__ = [
    "DEFAULT_MODELS",
    "LLM",
    "AssistantMessage",
    "LLMRefusal",
    "LLMUnavailable",
    "Message",
    "Price",
    "TextDelta",
    "ToolCall",
    "ToolResult",
    "ToolResultsMessage",
    "ToolSpec",
    "TurnResult",
    "Usage",
    "UserMessage",
    "create_llm",
    "price_for",
    "resolve_price",
]


def create_llm(settings: Settings) -> LLM:
    """Build the configured provider. SDK imports stay lazy so unused providers cost nothing."""
    provider, model = settings.llm_provider, settings.model
    if provider == "anthropic":
        from app.agent.llm.anthropic_llm import AnthropicLLM

        return AnthropicLLM(settings.anthropic_api_key, model, settings.llm_effort, settings.llm_max_tokens)
    if provider == "gemini":
        from app.agent.llm.gemini_llm import GeminiLLM

        return GeminiLLM(settings.gemini_api_key, model, settings.llm_max_tokens)
    if provider == "ollama":
        from app.agent.llm.ollama_llm import OllamaLLM

        return OllamaLLM(settings.ollama_base_url, model, settings.ollama_num_ctx)
    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r} (expected anthropic, gemini or ollama)")


def resolve_price(settings: Settings) -> Price | None:
    """PRICE_* env overrides win; otherwise the built-in table. None means unknown."""
    base = price_for(settings.llm_provider, settings.model)
    overrides = (
        settings.price_input_per_mtok,
        settings.price_output_per_mtok,
        settings.price_cache_write_per_mtok,
        settings.price_cache_read_per_mtok,
    )
    if all(v is None for v in overrides):
        return base
    fallback = base or Price(0, 0, 0, 0)
    return Price(
        input=overrides[0] if overrides[0] is not None else fallback.input,
        output=overrides[1] if overrides[1] is not None else fallback.output,
        cache_write=overrides[2] if overrides[2] is not None else fallback.cache_write,
        cache_read=overrides[3] if overrides[3] is not None else fallback.cache_read,
    )
