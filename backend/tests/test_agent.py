"""Agent loop behaviour with a scripted fake LLM (no network, deterministic)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.agent.llm import (
    AssistantMessage,
    LLMRefusal,
    LLMUnavailable,
    Price,
    TextDelta,
    ToolCall,
    ToolResultsMessage,
    TurnResult,
    Usage,
    UserMessage,
)
from app.agent.runner import HistoryTurn, QuestionAnswerer

pytestmark = pytest.mark.usefixtures("indexed_repo")


class ScriptedLLM:
    """Plays back a list of turns and records what it was asked."""

    provider = "scripted"
    model = "fake-1"

    def __init__(self, turns: list[TurnResult | Exception]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def stream_turn(self, *, system, messages, tools, allow_tools) -> AsyncIterator[TextDelta | TurnResult]:
        self.calls.append({"messages": list(messages), "tools": tools, "allow_tools": allow_tools})
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        for word in turn.text.split(" "):
            yield TextDelta(word + " ")
        yield turn


def tool_turn(*calls: tuple[str, dict]) -> TurnResult:
    return TurnResult(
        message=AssistantMessage("", [ToolCall(f"t{i}", n, a) for i, (n, a) in enumerate(calls)], raw="raw-blocks"),
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=10),
    )


def answer_turn(text: str) -> TurnResult:
    return TurnResult(
        message=AssistantMessage(text),
        stop_reason="end_turn",
        usage=Usage(input_tokens=200, output_tokens=50),
    )


async def collect(answerer, repo, question="How is tax applied?", mode="deep", history=None):
    return [e async for e in answerer.answer(repo, question, history or [], mode)]


def make(store, embedder, settings, turns):
    llm = ScriptedLLM(turns)
    return QuestionAnswerer(store, embedder, llm, settings), llm


async def test_deep_mode_runs_tools_then_answers_with_checked_citations(store, embedder, settings, indexed_repo):
    answerer, llm = make(
        store,
        embedder,
        settings,
        [
            tool_turn(
                ("search_code", {"query": "tax"}),
                ("read_file", {"path": "shop/pricing.py", "start_line": 27, "end_line": 29}),
            ),
            answer_turn("Tax is added in total_with_tax [shop/pricing.py:L27-L29] and not in [shop/api.py:L1-L2]."),
        ],
    )
    events = await collect(answerer, indexed_repo)

    steps = [e for e in events if e["type"] == "step"]
    assert [s["tool"] for s in steps] == ["search_code", "read_file"]
    done = events[-1]
    assert done["type"] == "done"
    cites = {c["path"]: c for c in done["citations"]}
    assert cites["shop/pricing.py"]["valid"] and cites["shop/pricing.py"]["grounded"]
    assert cites["shop/api.py"]["valid"] and not cites["shop/api.py"]["grounded"]
    assert done["usage"]["input_tokens"] == 300 and done["usage"]["output_tokens"] == 60

    # Both tool results went back in a single user message, matched by id.
    sent = llm.calls[1]["messages"]
    assert sent[-2].raw == "raw-blocks"  # the provider's own turn is replayed unchanged
    assert isinstance(sent[-1], ToolResultsMessage)
    assert [(r.call_id, r.name) for r in sent[-1].results] == [("t0", "search_code"), ("t1", "read_file")]


async def test_last_step_forbids_tools_so_the_loop_always_ends(store, embedder, settings, indexed_repo):
    settings = settings.model_copy(update={"agent_max_steps": 3})
    answerer, llm = make(
        store,
        embedder,
        settings,
        [
            tool_turn(("list_files", {"path_prefix": ""})),
            tool_turn(("list_files", {"path_prefix": "shop"})),
            answer_turn("Done."),
        ],
    )
    events = await collect(answerer, indexed_repo)
    assert [c["allow_tools"] for c in llm.calls] == [True, True, False]
    assert events[-1]["type"] == "done"


async def test_quick_mode_is_one_call_with_retrieved_context(store, embedder, settings, indexed_repo):
    answerer, llm = make(store, embedder, settings, [answer_turn("Discounts use apply_discount.")])
    events = await collect(answerer, indexed_repo, question="how are discounts applied", mode="quick")
    assert len(llm.calls) == 1 and llm.calls[0]["tools"] == []
    assert "<context>" in llm.calls[0]["messages"][-1].text
    assert "".join(e["delta"] for e in events if e["type"] == "text").strip() == "Discounts use apply_discount."


async def test_history_is_sent_as_plain_turns(store, embedder, settings, indexed_repo):
    answerer, llm = make(store, embedder, settings, [answer_turn("ok")])
    await collect(answerer, indexed_repo, mode="quick", history=[HistoryTurn("first?", "first answer")])
    kinds = [type(m) for m in llm.calls[0]["messages"]]
    assert kinds == [UserMessage, AssistantMessage, UserMessage]


async def test_refusal_and_failures_become_error_events_and_are_logged(store, embedder, settings, indexed_repo):
    cases = [
        (LLMRefusal("declined"), "declined"),
        (LLMUnavailable("Could not reach Ollama"), "Could not reach Ollama"),
        (RuntimeError("boom"), "Something went wrong"),
    ]
    for exc, expected in cases:
        answerer, _ = make(store, embedder, settings, [exc])
        events = await collect(answerer, indexed_repo, question=f"q {expected}")
        assert events[-1]["type"] == "error" and expected in events[-1]["message"]
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT error FROM query_logs WHERE question LIKE 'q %%'").fetchall()
    assert len(rows) == 3 and all(r["error"] for r in rows)


async def test_cost_uses_the_price_table_and_logs_cache_tokens(store, embedder, settings, indexed_repo):
    turn = answer_turn("Priced.")
    turn.usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_write_tokens=0, cache_read_tokens=500_000)
    answerer = QuestionAnswerer(store, embedder, ScriptedLLM([turn]), settings, Price(4.0, 20.0, 5.0, 0.2))
    done = (await collect(answerer, indexed_repo, question="priced question", mode="quick"))[-1]
    assert done["usage"]["cost_usd"] == 4.0 + 2.0 + 0.1
    assert done["model"] == "scripted/fake-1"
    with store.pool.connection() as conn:
        row = conn.execute("SELECT * FROM query_logs WHERE question = 'priced question'").fetchone()
    assert row["cache_read_tokens"] == 500_000 and row["provider"] == "scripted" and row["model"] == "fake-1"


async def test_unknown_price_reports_zero_cost(store, embedder, settings, indexed_repo):
    answerer = QuestionAnswerer(store, embedder, ScriptedLLM([answer_turn("x")]), settings, price=None)
    done = (await collect(answerer, indexed_repo, mode="quick"))[-1]
    assert done["usage"]["cost_usd"] == 0.0 and done["usage"]["priced"] is False
