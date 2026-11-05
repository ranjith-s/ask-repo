"""Provider adapters, tested offline: real SDKs/HTTP clients against canned responses."""

import json

import httpx
import httpx2  # the Anthropic SDK's HTTP client
import pytest
from google.genai import types

from app.agent.llm import (
    AssistantMessage,
    LLMRefusal,
    LLMUnavailable,
    Price,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    ToolSpec,
    TurnResult,
    UserMessage,
    create_llm,
    resolve_price,
)
from app.agent.llm.anthropic_llm import AnthropicLLM, to_anthropic_messages
from app.agent.llm.gemini_llm import GeminiLLM, to_gemini_contents
from app.agent.llm.ollama_llm import OllamaLLM, to_ollama_messages
from app.agent.tools import TOOL_SPECS
from app.config import Settings

TOOLS = [ToolSpec("grep", "find text", {"type": "object", "properties": {"pattern": {"type": "string"}}})]
CONVERSATION = [
    UserMessage("How is tax applied?"),
    AssistantMessage("", [ToolCall("c1", "grep", {"pattern": "tax"})], raw=None),
    ToolResultsMessage([ToolResult("c1", "grep", "pricing.py:L5: TAX_RATE", False)]),
]


async def collect(stream) -> tuple[str, TurnResult]:
    text, result = "", None
    async for item in stream:
        if isinstance(item, TextDelta):
            text += item.text
        else:
            result = item
    assert result is not None
    return text, result


# ---- Anthropic -----------------------------------------------------------------


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def anthropic_stream(blocks: list[dict], stop_reason: str, usage: dict) -> bytes:
    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": usage["input"],
                    "output_tokens": 0,
                    "cache_read_input_tokens": usage["read"],
                    "cache_creation_input_tokens": usage["write"],
                },
            },
        }
    ]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            events.append(
                {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}}
            )
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            events.append({"type": "content_block_start", "index": i, "content_block": start})
            events.append(
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
                }
            )
        events.append({"type": "content_block_stop", "index": i})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage["output"]},
        }
    )
    events.append({"type": "message_stop"})
    return sse(*events)


def anthropic_llm(body: bytes, captured: list[dict], status: int = 200) -> AnthropicLLM:
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return httpx2.Response(status, content=body, headers={"content-type": "text/event-stream"})

    llm = AnthropicLLM("test-key", "claude-opus-5-5", "medium", 1000)
    llm.client = llm.client.with_options(
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), max_retries=0
    )
    return llm


def test_history_breakpoint_only_on_last_block_and_only_when_asked():
    with_cache = to_anthropic_messages(CONVERSATION, cache_history=True)
    marked = [b for m in with_cache for b in m["content"] if isinstance(b, dict) and "cache_control" in b]
    assert marked == [with_cache[-1]["content"][-1]]
    assert marked[0]["type"] == "tool_result" and marked[0]["tool_use_id"] == "c1"
    without = to_anthropic_messages(CONVERSATION, cache_history=False)
    assert not any("cache_control" in b for m in without for b in m["content"] if isinstance(b, dict))


def test_assistant_raw_blocks_are_replayed_unchanged():
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "c1"}]
    msgs = to_anthropic_messages([UserMessage("q"), AssistantMessage("", raw=raw)], cache_history=False)
    assert msgs[1] == {"role": "assistant", "content": raw}


def test_quick_mode_request_does_not_cache_the_one_off_context():
    params = AnthropicLLM("k", "claude-opus-5-5", "low", 100).build_params("sys", [UserMessage("q")], [], False)
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in params["messages"][-1]["content"][-1]
    assert "tools" not in params and params["output_config"] == {"effort": "low"}


def test_deep_mode_last_step_forbids_tools_and_tools_are_strict():
    params = AnthropicLLM("k", "claude-opus-5-5", "low", 100).build_params("sys", CONVERSATION, TOOLS, False)
    assert params["tool_choice"] == {"type": "none"}
    assert params["tools"][0]["strict"] is True and params["tools"][0]["input_schema"] == TOOLS[0].parameters


async def test_anthropic_streams_text_and_maps_cache_usage():
    captured: list[dict] = []
    body = anthropic_stream(
        [{"type": "text", "text": "Tax is 18%."}], "end_turn", {"input": 120, "read": 3000, "write": 400, "output": 25}
    )
    llm = anthropic_llm(body, captured)
    text, result = await collect(llm.stream_turn(system="sys", messages=CONVERSATION, tools=TOOLS, allow_tools=True))
    assert text == "Tax is 18%." and result.text == "Tax is 18%." and not result.tool_calls
    assert (
        result.usage.input_tokens,
        result.usage.cache_read_tokens,
        result.usage.cache_write_tokens,
        result.usage.output_tokens,
    ) == (120, 3000, 400, 25)
    sent = captured[0]
    assert sent["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert sent["fallbacks"] == "default" and sent["thinking"] == {"type": "adaptive"}


async def test_anthropic_tool_use_is_parsed_and_raw_kept():
    body = anthropic_stream(
        [{"type": "tool_use", "id": "tu_1", "name": "grep", "input": {"pattern": "tax"}}],
        "tool_use",
        {"input": 10, "read": 0, "write": 0, "output": 5},
    )
    _, result = await collect(
        anthropic_llm(body, []).stream_turn(system="s", messages=[UserMessage("q")], tools=TOOLS, allow_tools=True)
    )
    assert result.tool_calls == [ToolCall("tu_1", "grep", {"pattern": "tax"})]
    assert result.message.raw[0].type == "tool_use"


async def test_anthropic_refusal_and_auth_errors_become_typed_errors():
    body = anthropic_stream([], "refusal", {"input": 1, "read": 0, "write": 0, "output": 0})
    with pytest.raises(LLMRefusal):
        await collect(
            anthropic_llm(body, []).stream_turn(system="s", messages=[UserMessage("q")], tools=[], allow_tools=False)
        )
    error = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}).encode()
    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        await collect(
            anthropic_llm(error, [], status=401).stream_turn(
                system="s", messages=[UserMessage("q")], tools=[], allow_tools=False
            )
        )


# ---- Gemini --------------------------------------------------------------------


class FakeGeminiModels:
    def __init__(self, chunks: list[types.GenerateContentResponse]) -> None:
        self.chunks = chunks
        self.calls: list[dict] = []

    async def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)

        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


class FakeGeminiClient:
    def __init__(self, chunks: list[types.GenerateContentResponse]) -> None:
        self.models = FakeGeminiModels(chunks)
        self.aio = self


def gemini_chunk(*parts: types.Part, finish: str | None = None, usage: dict | None = None):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=types.FinishReason(finish) if finish else None,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(**usage) if usage else None,
    )


def test_gemini_contents_roles_and_function_responses():
    contents = to_gemini_contents(CONVERSATION)
    assert [c.role for c in contents] == ["user", "model", "user"]
    response = contents[2].parts[0].function_response
    assert (
        response.name == "grep" and response.id == "c1" and response.response == {"result": "pricing.py:L5: TAX_RATE"}
    )
    raw = types.Content(role="model", parts=[types.Part(text="kept", thought_signature=b"sig")])
    assert to_gemini_contents([AssistantMessage("x", raw=raw)])[0] is raw  # thought signatures survive


async def test_gemini_streams_text_skips_thoughts_and_maps_usage():
    client = FakeGeminiClient(
        [
            gemini_chunk(types.Part(text="planning…", thought=True)),
            gemini_chunk(
                types.Part(text="Tax is "),
                types.Part(text="18%."),
                finish="STOP",
                usage={
                    "prompt_token_count": 1000,
                    "cached_content_token_count": 600,
                    "candidates_token_count": 40,
                    "thoughts_token_count": 60,
                },
            ),
        ]
    )
    llm = GeminiLLM("key", "gemini-2.5-flash", 1000, client=client)
    text, result = await collect(
        llm.stream_turn(system="sys", messages=[UserMessage("q")], tools=[], allow_tools=False)
    )
    assert text == "Tax is 18%." and result.text == "Tax is 18%."
    assert (result.usage.input_tokens, result.usage.cache_read_tokens, result.usage.output_tokens) == (400, 600, 100)
    assert client.models.calls[0]["config"].system_instruction == "sys"


async def test_gemini_function_calls_get_ids_and_last_step_disables_tools():
    call = types.Part(function_call=types.FunctionCall(name="grep", args={"pattern": "tax"}))
    client = FakeGeminiClient([gemini_chunk(call, finish="STOP")])
    llm = GeminiLLM("key", "gemini-2.5-flash", 1000, client=client)
    _, result = await collect(
        llm.stream_turn(system="s", messages=[UserMessage("q")], tools=TOOL_SPECS, allow_tools=True)
    )
    assert result.tool_calls[0].name == "grep" and result.tool_calls[0].input == {"pattern": "tax"}
    assert result.message.raw.parts[0].function_call.id == result.tool_calls[0].id  # replay matches our result id
    declared = client.models.calls[0]["config"].tools[0].function_declarations
    assert [d.name for d in declared] == [t.name for t in TOOL_SPECS]

    _ = [x async for x in llm.stream_turn(system="s", messages=[UserMessage("q")], tools=TOOL_SPECS, allow_tools=False)]
    mode = client.models.calls[1]["config"].tool_config.function_calling_config.mode
    assert mode == types.FunctionCallingConfigMode.NONE


async def test_gemini_safety_block_is_a_refusal():
    llm = GeminiLLM("key", "m", 100, client=FakeGeminiClient([gemini_chunk(types.Part(text=""), finish="SAFETY")]))
    with pytest.raises(LLMRefusal):
        await collect(llm.stream_turn(system="s", messages=[UserMessage("q")], tools=[], allow_tools=False))


# ---- Ollama --------------------------------------------------------------------


def ndjson(*objs: dict) -> bytes:
    return "".join(json.dumps(o) + "\n" for o in objs).encode()


def ollama_llm(body: bytes, captured: list[dict], status: int = 200) -> OllamaLLM:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(status, content=body)

    return OllamaLLM("http://ollama:11434", "qwen3:8b", 8192, transport=httpx.MockTransport(handler))


def test_ollama_messages_use_system_role_and_tool_name():
    msgs = to_ollama_messages("sys", CONVERSATION)
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[-1] == {"role": "tool", "content": "pricing.py:L5: TAX_RATE", "tool_name": "grep"}


async def test_ollama_streams_text_tool_calls_and_usage():
    captured: list[dict] = []
    body = ndjson(
        {"message": {"role": "assistant", "content": "Let me check. "}, "done": False},
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "grep", "arguments": {"pattern": "tax"}}},
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "a.py", "start_line": 1, "end_line": 5}',
                        }
                    },
                ],
            },
            "done": False,
        },
        {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 900, "eval_count": 40},
    )
    text, result = await collect(
        ollama_llm(body, captured).stream_turn(system="sys", messages=CONVERSATION, tools=TOOLS, allow_tools=True)
    )
    assert text == "Let me check. "
    assert [(c.name, c.input) for c in result.tool_calls] == [
        ("grep", {"pattern": "tax"}),
        ("read_file", {"path": "a.py", "start_line": 1, "end_line": 5}),
    ]
    assert len({c.id for c in result.tool_calls}) == 2
    assert (result.usage.input_tokens, result.usage.output_tokens) == (900, 40)
    assert result.message.raw["tool_calls"]  # replayed as the assistant turn
    assert captured[0]["options"]["num_ctx"] == 8192 and captured[0]["tools"][0]["function"]["name"] == "grep"


async def test_ollama_last_step_sends_no_tools():
    captured: list[dict] = []
    body = ndjson({"message": {"role": "assistant", "content": "Answer"}, "done": True})
    await collect(
        ollama_llm(body, captured).stream_turn(system="s", messages=[UserMessage("q")], tools=TOOLS, allow_tools=False)
    )
    assert "tools" not in captured[0]


async def test_ollama_errors_are_actionable():
    with pytest.raises(LLMUnavailable, match="ollama pull qwen3:8b"):
        await collect(
            ollama_llm(b'{"error":"model not found"}', [], status=404).stream_turn(
                system="s", messages=[UserMessage("q")], tools=[], allow_tools=False
            )
        )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    llm = OllamaLLM("http://ollama:11434", "m", 100, transport=httpx.MockTransport(refuse))
    with pytest.raises(LLMUnavailable, match="ollama serve"):
        await collect(llm.stream_turn(system="s", messages=[UserMessage("q")], tools=[], allow_tools=False))


# ---- Factory, config and pricing ----------------------------------------------------


@pytest.mark.parametrize(
    "provider,cls,model",
    [
        ("anthropic", AnthropicLLM, "claude-opus-5-5"),
        ("gemini", GeminiLLM, "gemini-3.5-flash"),
        ("ollama", OllamaLLM, "qwen3:8b"),
    ],
)
def test_create_llm_picks_provider_and_default_model(provider, cls, model):
    llm = create_llm(Settings(llm_provider=provider, anthropic_api_key="k", gemini_api_key="k"))
    assert isinstance(llm, cls) and llm.model == model


def test_llm_problem_explains_missing_configuration():
    assert "ANTHROPIC_API_KEY" in Settings(llm_provider="anthropic", anthropic_api_key=None).llm_problem()
    assert "GEMINI_API_KEY" in Settings(llm_provider="gemini", gemini_api_key=None).llm_problem()
    assert Settings(llm_provider="ollama").llm_problem() is None
    assert "Unknown" in Settings(llm_provider="openai").llm_problem()
    with pytest.raises(ValueError):
        create_llm(Settings(llm_provider="openai"))


def test_prices_come_from_table_env_overrides_or_are_unknown():
    assert resolve_price(Settings(llm_provider="anthropic")) == Price(4.0, 20.0, 5.0, 0.2)
    assert resolve_price(Settings(llm_provider="ollama")) == Price(0, 0, 0, 0)
    assert resolve_price(Settings(llm_provider="gemini", llm_model="gemini-9-ultra")) is None
    custom = resolve_price(Settings(llm_provider="gemini", llm_model="gemini-9-ultra", price_input_per_mtok=1.5))
    assert custom == Price(1.5, 0, 0, 0)
