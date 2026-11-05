"""Local models through Ollama's native /api/chat (streaming NDJSON, tool calls).

No API key and no cost. Quality depends heavily on the model: small models
call tools less reliably, so quick mode is often the better fit locally.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agent.llm.base import (
    AssistantMessage,
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


def to_ollama_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in tools
    ]


def to_ollama_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if isinstance(m, UserMessage):
            out.append({"role": "user", "content": m.text})
        elif isinstance(m, AssistantMessage):
            if isinstance(m.raw, dict):
                out.append(m.raw)
            else:
                out.append({"role": "assistant", "content": m.text})
        elif isinstance(m, ToolResultsMessage):
            out.extend({"role": "tool", "content": r.content, "tool_name": r.name} for r in m.results)
    return out


class OllamaLLM:
    provider = "ollama"

    def __init__(self, base_url: str, model: str, num_ctx: int, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.transport = transport  # injectable for tests

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_tools: bool,
    ) -> AsyncIterator[TextDelta | TurnResult]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": to_ollama_messages(system, messages),
            "stream": True,
            "options": {"num_ctx": self.num_ctx},
        }
        # Ollama has no "tool_choice: none"; leaving tools out forces a text answer.
        if tools and allow_tools:
            body["tools"] = to_ollama_tools(tools)

        text, calls, raw_calls, usage = "", [], [], Usage()
        try:
            async with (
                httpx.AsyncClient(
                    base_url=self.base_url, timeout=httpx.Timeout(300, connect=5), transport=self.transport
                ) as client,
                client.stream("POST", "/api/chat", json=body) as resp,
            ):
                if resp.status_code == 404:
                    raise LLMUnavailable(f"Ollama model '{self.model}' not found. Run: ollama pull {self.model}")
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:200]
                    raise LLMUnavailable(f"Ollama error {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise LLMUnavailable(f"Ollama error: {chunk['error']}")
                    msg = chunk.get("message") or {}
                    if piece := msg.get("content"):
                        text += piece
                        yield TextDelta(piece)
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):  # some models emit a JSON string
                            args = json.loads(args or "{}")
                        calls.append(ToolCall(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", fn.get("name", ""), args))
                        raw_calls.append(tc)
                    if chunk.get("done"):
                        usage = Usage(
                            input_tokens=chunk.get("prompt_eval_count", 0), output_tokens=chunk.get("eval_count", 0)
                        )
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"Could not reach Ollama at {self.base_url}. Is `ollama serve` running?") from exc

        raw: dict[str, Any] = {"role": "assistant", "content": text}
        if raw_calls:
            raw["tool_calls"] = raw_calls
        yield TurnResult(
            message=AssistantMessage(text=text, tool_calls=calls, raw=raw), stop_reason="stop", usage=usage
        )
