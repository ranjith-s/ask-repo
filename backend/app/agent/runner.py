"""Answer a question about a repo, streaming events for the UI.

Two modes:
- quick: retrieve once (hybrid search), answer in one LLM call. Fast and cheap.
- deep:  an agent loop where the model calls search/grep/read tools until it has
         enough evidence. Better on questions that span several files.

Events: step (a tool ran), text (answer delta), done (citations, usage), error.
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from app.agent.citations import check_citations
from app.agent.llm import (
    LLM,
    AssistantMessage,
    LLMRefusal,
    LLMUnavailable,
    Message,
    Price,
    TextDelta,
    ToolResult,
    ToolResultsMessage,
    ToolSpec,
    TurnResult,
    Usage,
    UserMessage,
)
from app.agent.prompts import AGENT_SYSTEM, QUICK_SYSTEM, format_context
from app.agent.tools import TOOL_SPECS, RepoTools, repo_map
from app.config import Settings
from app.embeddings import Embedder
from app.observability import log
from app.store import RepoStore

Mode = Literal["quick", "deep"]
Event = dict[str, Any]


@dataclass
class HistoryTurn:
    question: str
    answer: str


class QuestionAnswerer:
    def __init__(
        self, store: RepoStore, embedder: Embedder, llm: LLM, settings: Settings, price: Price | None = None
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.settings = settings
        self.price = price  # None: unknown model, cost reported as 0

    def _cost(self, usage: Usage) -> float:
        return round(self.price.cost(usage), 5) if self.price else 0.0

    async def answer(
        self,
        repo: dict[str, Any],
        question: str,
        history: list[HistoryTurn],
        mode: Mode,
    ) -> AsyncIterator[Event]:
        started = time.perf_counter()
        tools = RepoTools(self.store, self.embedder, repo["id"], self.settings.tool_result_max_chars)
        usage = Usage()
        steps: list[Event] = []
        answer, error = "", None
        try:
            system = self._system(repo, AGENT_SYSTEM if mode == "deep" else QUICK_SYSTEM)
            messages = self._history_messages(history)
            run = self._deep if mode == "deep" else self._quick
            async for event in run(tools, system, messages, question, usage):
                if event["type"] == "step":
                    steps.append(event)
                elif event["type"] == "final":
                    answer = event["text"]
                    continue
                yield event

            citations = check_citations(answer, self.store.file_line_counts(repo["id"]), tools.seen)
            yield {
                "type": "done",
                "citations": [c.to_dict() for c in citations],
                "usage": usage.__dict__ | {"cost_usd": self._cost(usage), "priced": self.price is not None},
                "model": f"{self.llm.provider}/{self.llm.model}",
                "latency_ms": _ms(started),
            }
        except (LLMRefusal, LLMUnavailable) as exc:
            error = str(exc)
            log.warning("answer.llm_error", repo_id=repo["id"], error=error, provider=self.llm.provider)
            yield {"type": "error", "message": error}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("answer.failed", repo_id=repo["id"])
            yield {
                "type": "error",
                "message": (
                    "Something went wrong while answering. Please try again; "
                    "if it keeps happening, check the server logs."
                ),
            }
        finally:
            log.info(
                "answer.done",
                repo_id=repo["id"],
                mode=mode,
                steps=len(steps),
                latency_ms=_ms(started),
                error=error,
                provider=self.llm.provider,
                model=self.llm.model,
                **usage.__dict__,
            )
            self.store.log_query(
                repo_id=repo["id"],
                question=question,
                mode=mode,
                steps=steps,
                provider=self.llm.provider,
                model=self.llm.model,
                input_tokens=usage.input_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=self._cost(usage),
                latency_ms=_ms(started),
                error=error,
            )

    async def _quick(
        self,
        tools: RepoTools,
        system: str,
        messages: list[Message],
        question: str,
        usage: Usage,
    ) -> AsyncIterator[Event]:
        t0 = time.perf_counter()
        hits = tools.search(question, limit=10)
        yield {
            "type": "step",
            "tool": "search_code",
            "summary": f"Found the {len(hits)} most relevant parts of the code",
            "duration_ms": _ms(t0),
            "locations": [{"path": h.path, "start": h.start_line, "end": h.end_line} for h in hits],
        }
        messages.append(UserMessage(f"{format_context(hits)}\n\nQuestion: {question}"))
        async for event in self._turn(system, messages, [], allow_tools=False, usage=usage):
            if event["type"] == "turn":
                yield {"type": "final", "text": event["result"].text}
            else:
                yield event

    async def _deep(
        self,
        tools: RepoTools,
        system: str,
        messages: list[Message],
        question: str,
        usage: Usage,
    ) -> AsyncIterator[Event]:
        messages.append(UserMessage(question))
        max_steps = self.settings.agent_max_steps
        for step in range(max_steps):
            turn: TurnResult | None = None
            async for event in self._turn(system, messages, TOOL_SPECS, allow_tools=step < max_steps - 1, usage=usage):
                if event["type"] == "turn":
                    turn = event["result"]
                else:
                    yield event
            assert turn is not None
            if not turn.tool_calls:
                yield {"type": "final", "text": turn.text}
                return
            messages.append(turn.message)
            results: list[ToolResult] = []
            for call in turn.tool_calls:  # all results go back in ONE user message
                t0 = time.perf_counter()
                outcome = await tools.run(call.name, call.input)
                yield {
                    "type": "step",
                    "tool": call.name,
                    "input": call.input,
                    "summary": outcome.summary,
                    "is_error": outcome.is_error,
                    "duration_ms": _ms(t0),
                    "locations": outcome.locations,
                }
                results.append(ToolResult(call.id, call.name, outcome.content, outcome.is_error))
            messages.append(ToolResultsMessage(results))

    async def _turn(
        self,
        system: str,
        messages: list[Message],
        tool_defs: list[ToolSpec],
        allow_tools: bool,
        usage: Usage,
    ) -> AsyncIterator[Event]:
        """One model call. Text is streamed; a turn that ends in tool calls is not the answer."""
        async for item in self.llm.stream_turn(
            system=system, messages=messages, tools=tool_defs, allow_tools=allow_tools
        ):
            if isinstance(item, TextDelta):
                yield {"type": "text", "delta": item.text}
            else:
                usage.add(item.usage)
                yield {"type": "turn", "result": item}

    def _system(self, repo: dict[str, Any], template: str) -> str:
        """Stable per repo (no timestamps or ids), so providers can cache it across questions."""
        paths = self.store.list_paths(repo["id"])
        return template.format(
            name=repo["name"], commit=(repo.get("commit_sha") or "local")[:10], repo_map=repo_map(paths)
        )

    def _history_messages(self, history: list[HistoryTurn]) -> list[Message]:
        """Only previous Q&A text is kept, not tool traffic: cheap, and enough for follow-ups."""
        messages: list[Message] = []
        for turn in history[-self.settings.history_max_turns :]:
            messages.append(UserMessage(turn.question))
            messages.append(AssistantMessage(turn.answer))
        return messages


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
