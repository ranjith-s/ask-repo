"""Claude via the Anthropic SDK: streaming, tool use, adaptive thinking, prompt caching."""

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.agent.llm.base import (
    AssistantMessage,
    LLMRefusal,
    LLMUnavailable,
    Message,
    TextDelta,
    ToolCall,
    ToolResultsMessage,
    ToolSpec,
    TurnResult,
    Usage,
    UserMessage,
)

CACHE = {"type": "ephemeral"}


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters, "strict": True} for t in tools]


def to_anthropic_messages(messages: list[Message], cache_history: bool) -> list[dict[str, Any]]:
    """Convert neutral messages; optionally mark the end of the conversation for caching.

    Prompt caching is prefix-based. In the agent loop each request resends the
    whole conversation plus one new step, so a breakpoint on the last block lets
    the next request read everything before it from cache (5% of the input price
    on Opus 5.5) and pay full price only for the new step.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, UserMessage):
            out.append({"role": "user", "content": [{"type": "text", "text": m.text}]})
        elif isinstance(m, AssistantMessage):
            # Replay the raw blocks (thinking + tool_use) unchanged when we have them.
            content = m.raw if m.raw is not None else [{"type": "text", "text": m.text or "(no answer)"}]
            out.append({"role": "assistant", "content": content})
        elif isinstance(m, ToolResultsMessage):
            out.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                        for r in m.results
                    ],
                }
            )
    if cache_history and out and out[-1]["role"] == "user":
        last_block = out[-1]["content"][-1]
        last_block["cache_control"] = CACHE
    return out


class AnthropicLLM:
    provider = "anthropic"

    def __init__(self, api_key: str | None, model: str, effort: str, max_tokens: int) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def build_params(
        self, system: str, messages: list[Message], tools: list[ToolSpec], allow_tools: bool
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # Breakpoint 1: tools + system prompt, identical for every question on a repo.
            "system": [{"type": "text", "text": system, "cache_control": CACHE}],
            # Breakpoint 2: the conversation so far, only in the multi-step agent loop.
            # A single-call quick answer would pay the 1.25x write and never read it back.
            "messages": to_anthropic_messages(messages, cache_history=bool(tools)),
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            # On a safety-classifier refusal, let the API retry on a fallback model.
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if tools:
            params["tools"] = to_anthropic_tools(tools)
            if not allow_tools:  # last step: force a written answer
                params["tool_choice"] = {"type": "none"}
        return params

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_tools: bool,
    ) -> AsyncIterator[TextDelta | TurnResult]:
        params = self.build_params(system, messages, tools, allow_tools)
        try:
            async with self.client.beta.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield TextDelta(event.text)
                message = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable("Claude rejected the API key. Check ANTHROPIC_API_KEY in .env.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("Claude is busy right now (rate limit). Please try again in a minute.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable("Couldn't reach Claude. Check your internet connection.") from exc

        if message.stop_reason == "refusal":
            raise LLMRefusal("The AI declined to answer this question. Try rephrasing it.")
        u = message.usage
        yield TurnResult(
            message=AssistantMessage(
                text="".join(b.text for b in message.content if b.type == "text"),
                tool_calls=[ToolCall(b.id, b.name, dict(b.input)) for b in message.content if b.type == "tool_use"],
                raw=list(message.content),
            ),
            stop_reason=message.stop_reason,
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_write_tokens=u.cache_creation_input_tokens or 0,
                cache_read_tokens=u.cache_read_input_tokens or 0,
            ),
        )
