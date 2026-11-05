"""Provider-neutral types for the agent loop.

The runner only speaks in these types. Each provider adapter converts them to
its own wire format and back, so the loop, the tools and the tests never change
when a provider is added.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class UserMessage:
    text: str


@dataclass
class AssistantMessage:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # The provider's own representation of this turn (thinking blocks, thought
    # signatures). Passed back unchanged so multi-step reasoning keeps working.
    raw: Any = None


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class ToolResultsMessage:
    results: list[ToolResult]


Message = UserMessage | AssistantMessage | ToolResultsMessage


@dataclass
class TextDelta:
    text: str


@dataclass
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0  # includes thinking tokens where the provider bills them as output
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


@dataclass
class TurnResult:
    message: AssistantMessage
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls


class LLMRefusal(RuntimeError):
    """The provider declined to answer (safety filter)."""


class LLMUnavailable(RuntimeError):
    """The provider could not be reached or rejected the request; the message is safe to show."""


class LLM(Protocol):
    provider: str
    model: str

    def stream_turn(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_tools: bool,
    ) -> AsyncIterator[TextDelta | TurnResult]:
        """Stream one model turn: zero or more TextDelta, then exactly one TurnResult."""
        ...
