"""Gemini via the Google Gen AI SDK: streaming and function calling.

Gemini caches repeated prompt prefixes implicitly (no markers, no write fee);
cached tokens are reported in usage and billed at the lower cache-read rate.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

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

BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def to_gemini_tools(tools: list[ToolSpec]) -> list[Any]:  # list[Tool]; Any matches the SDK union
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in tools
            ]
        )
    ]


def to_gemini_contents(messages: list[Message]) -> list[types.Content]:
    out: list[types.Content] = []
    for m in messages:
        if isinstance(m, UserMessage):
            out.append(types.Content(role="user", parts=[types.Part(text=m.text)]))
        elif isinstance(m, AssistantMessage):
            # Replay raw parts: they carry thought signatures Gemini needs between tool calls.
            if isinstance(m.raw, types.Content):
                out.append(m.raw)
            else:
                out.append(types.Content(role="model", parts=[types.Part(text=m.text or "(no answer)")]))
        elif isinstance(m, ToolResultsMessage):
            parts = []
            for r in m.results:
                part = types.Part.from_function_response(
                    name=r.name, response={"error": r.content} if r.is_error else {"result": r.content}
                )
                if part.function_response is not None:
                    part.function_response.id = r.call_id
                parts.append(part)
            out.append(types.Content(role="user", parts=parts))
    return out


def usage_from(meta: types.GenerateContentResponseUsageMetadata | None) -> Usage:
    if meta is None:
        return Usage()
    cached = meta.cached_content_token_count or 0
    return Usage(
        input_tokens=(meta.prompt_token_count or 0) - cached,
        output_tokens=(meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0),
        cache_read_tokens=cached,
    )


class GeminiLLM:
    provider = "gemini"

    def __init__(self, api_key: str | None, model: str, max_tokens: int, client: Any = None) -> None:
        self.client = client or genai.Client(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        allow_tools: bool,
    ) -> AsyncIterator[TextDelta | TurnResult]:
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=self.max_tokens,
            tools=to_gemini_tools(tools) if tools else None,
            tool_config=(
                types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE)
                )
                if tools and not allow_tools
                else None
            ),
        )
        parts: list[types.Part] = []
        meta = None
        finish = None
        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=self.model, contents=to_gemini_contents(messages), config=config
            )
            async for chunk in stream:
                meta = chunk.usage_metadata or meta
                if chunk.prompt_feedback and chunk.prompt_feedback.block_reason:
                    raise LLMRefusal("Gemini declined to answer this question. Try rephrasing it.")
                if not chunk.candidates:
                    continue
                candidate = chunk.candidates[0]
                finish = candidate.finish_reason or finish
                for part in (candidate.content.parts if candidate.content else None) or []:
                    parts.append(part)
                    if part.text and not part.thought:
                        yield TextDelta(part.text)
        except genai_errors.ClientError as exc:
            raise LLMUnavailable(
                f"Gemini rejected the request ({exc.code}). Check GEMINI_API_KEY and LLM_MODEL."
            ) from exc
        except genai_errors.ServerError as exc:
            raise LLMUnavailable("Gemini is unavailable right now. Please try again in a minute.") from exc

        finish_name = getattr(finish, "name", None)
        if finish_name in BLOCKED:
            raise LLMRefusal("Gemini declined to answer this question. Try rephrasing it.")

        calls = [
            ToolCall(
                p.function_call.id or f"call_{uuid.uuid4().hex[:8]}",
                p.function_call.name or "",
                dict(p.function_call.args or {}),
            )
            for p in parts
            if p.function_call
        ]
        # Make sure the ids we answer with match the replayed parts.
        for part, call in zip((p for p in parts if p.function_call), calls, strict=True):
            assert part.function_call is not None
            part.function_call.id = call.id
        yield TurnResult(
            message=AssistantMessage(
                text="".join(p.text or "" for p in parts if p.text and not p.thought),
                tool_calls=calls,
                raw=types.Content(role="model", parts=parts),
            ),
            stop_reason=finish_name,
            usage=usage_from(meta),
        )
