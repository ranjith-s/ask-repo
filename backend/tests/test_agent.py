"""Agent loop behaviour with a scripted fake LLM (no network, deterministic)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.agent.llm import LLMRefusal, TextDelta, ToolCall, TurnResult, Usage
from app.agent.runner import HistoryTurn, QuestionAnswerer

pytestmark = pytest.mark.usefixtures("indexed_repo")


class ScriptedLLM:
    """Plays back a list of turns and records what it was asked."""

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
        content=[{"type": "text", "text": "…"}],
        text="",
        tool_calls=[ToolCall(f"t{i}", n, a) for i, (n, a) in enumerate(calls)],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=10),
    )


def answer_turn(text: str) -> TurnResult:
    return TurnResult(
        content=[{"type": "text", "text": text}],
        text=text,
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
    assert done["usage"]["input_tokens"] == 300 and done["usage"]["cost_usd"] > 0

    # Both tool results went back in a single user message, matched by id.
    tool_results = llm.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in tool_results] == ["t0", "t1"]


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
    assert "<context>" in llm.calls[0]["messages"][-1]["content"]
    assert "".join(e["delta"] for e in events if e["type"] == "text").strip() == "Discounts use apply_discount."


async def test_history_is_sent_as_plain_turns(store, embedder, settings, indexed_repo):
    answerer, llm = make(store, embedder, settings, [answer_turn("ok")])
    await collect(answerer, indexed_repo, mode="quick", history=[HistoryTurn("first?", "first answer")])
    roles = [m["role"] for m in llm.calls[0]["messages"]]
    assert roles == ["user", "assistant", "user"]


async def test_refusal_and_failures_become_error_events_and_are_logged(store, embedder, settings, indexed_repo):
    for exc, expected in [(LLMRefusal("declined"), "declined"), (RuntimeError("boom"), "Something went wrong")]:
        answerer, _ = make(store, embedder, settings, [exc])
        events = await collect(answerer, indexed_repo, question=f"q {expected}")
        assert events[-1]["type"] == "error" and expected in events[-1]["message"]
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT error FROM query_logs WHERE question LIKE 'q %%'").fetchall()
    assert len(rows) == 2 and all(r["error"] for r in rows)
