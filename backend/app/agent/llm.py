"""Thin wrapper over the Anthropic SDK so the agent loop can be tested with a fake."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from app.config import Settings


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens

    def cost_usd(self, s: Settings) -> float:
        return (
            self.input_tokens * s.price_input_per_mtok
            + self.output_tokens * s.price_output_per_mtok
            + self.cache_write_tokens * s.price_cache_write_per_mtok
            + self.cache_read_tokens * s.price_cache_read_per_mtok
        ) / 1_000_000


@dataclass
class TurnResult:
    content: list[Any]  # raw assistant blocks, appended back to history unchanged
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)


class LLMRefusal(RuntimeError):
    pass


class LLM(Protocol):
    def stream_turn(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        allow_tools: bool,
    ) -> AsyncIterator[TextDelta | TurnResult]: ...


class AnthropicLLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def stream_turn(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        allow_tools: bool,
    ) -> AsyncIterator[TextDelta | TurnResult]:
        params: dict[str, Any] = {
            "model": self.settings.llm_model,
            "max_tokens": self.settings.llm_max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.settings.llm_effort},
            # On a safety-classifier refusal, let the API retry on a fallback model.
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if tools:
            params["tools"] = tools
            if not allow_tools:  # last step: force a written answer
                params["tool_choice"] = {"type": "none"}

        async with self.client.beta.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "text":
                    yield TextDelta(event.text)
            message = await stream.get_final_message()

        if message.stop_reason == "refusal":
            raise LLMRefusal("The model declined to answer this request.")
        u = message.usage
        yield TurnResult(
            content=list(message.content),
            text="".join(b.text for b in message.content if b.type == "text"),
            tool_calls=[ToolCall(b.id, b.name, dict(b.input)) for b in message.content if b.type == "tool_use"],
            stop_reason=message.stop_reason,
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_write_tokens=u.cache_creation_input_tokens or 0,
                cache_read_tokens=u.cache_read_input_tokens or 0,
            ),
        )
