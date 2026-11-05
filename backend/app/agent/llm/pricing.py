"""Default models and prices, used for the per-question cost shown in the UI.

Prices are USD per million tokens: (input, output, cache write, cache read).
They change; override with PRICE_* environment variables rather than editing
code. Unknown models are priced at 0 and a warning is logged once.
"""

from dataclasses import dataclass

from app.agent.llm.base import Usage

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "gemini": "gemini-3.5-flash",
    "ollama": "qwen3:8b",
}


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_write_tokens * self.cache_write
            + usage.cache_read_tokens * self.cache_read
        ) / 1_000_000


FREE = Price(0, 0, 0, 0)

PRICES: dict[str, Price] = {
    # Anthropic: cache writes are 1.25x input (5-minute TTL).
    "claude-opus-5-5": Price(4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 0.20),
    # Gemini: implicit caching has no write charge; thinking tokens bill as output.
    # Check https://ai.google.dev/gemini-api/docs/pricing before relying on these.
    "gemini-2.5-flash": Price(0.30, 2.50, 0.0, 0.03),
    "gemini-2.5-pro": Price(1.25, 10.00, 0.0, 0.125),
}


def price_for(provider: str, model: str) -> Price | None:
    """Built-in price, FREE for local models, None if unknown."""
    if provider == "ollama":
        return FREE
    return PRICES.get(model)
