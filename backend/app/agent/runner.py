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
from app.agent.llm import LLM, LLMRefusal, TextDelta, TurnResult, Usage
from app.agent.prompts import AGENT_SYSTEM, QUICK_SYSTEM, format_context
from app.agent.tools import TOOL_DEFINITIONS, RepoTools, repo_map
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
    def __init__(self, store: RepoStore, embedder: Embedder, llm: LLM, settings: Settings) -> None:
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.settings = settings

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
                "usage": usage.__dict__ | {"cost_usd": round(usage.cost_usd(self.settings), 5)},
                "latency_ms": _ms(started),
            }
        except LLMRefusal as exc:
            error = str(exc)
            yield {"type": "error", "message": error}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("answer.failed", repo_id=repo["id"])
            yield {"type": "error", "message": "Something went wrong while answering. Check the server logs."}
        finally:
            log.info(
                "answer.done",
                repo_id=repo["id"],
                mode=mode,
                steps=len(steps),
                latency_ms=_ms(started),
                error=error,
                **usage.__dict__,
            )
            self.store.log_query(
                repo_id=repo["id"],
                question=question,
                mode=mode,
                steps=steps,
                input_tokens=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd(self.settings),
                latency_ms=_ms(started),
                error=error,
            )

    async def _quick(
        self,
        tools: RepoTools,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        question: str,
        usage: Usage,
    ) -> AsyncIterator[Event]:
        t0 = time.perf_counter()
        hits = tools.search(question, limit=10)
        yield {
            "type": "step",
            "tool": "search_code",
            "summary": f"retrieved {len(hits)} chunks",
            "duration_ms": _ms(t0),
            "locations": [{"path": h.path, "start": h.start_line, "end": h.end_line} for h in hits],
        }
        messages.append({"role": "user", "content": f"{format_context(hits)}\n\nQuestion: {question}"})
        async for event in self._turn(system, messages, [], allow_tools=False, usage=usage):
            if event["type"] == "turn":
                yield {"type": "final", "text": event["result"].text}
            else:
                yield event

    async def _deep(
        self,
        tools: RepoTools,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        question: str,
        usage: Usage,
    ) -> AsyncIterator[Event]:
        messages.append({"role": "user", "content": question})
        max_steps = self.settings.agent_max_steps
        for step in range(max_steps):
            turn: TurnResult | None = None
            async for event in self._turn(
                system, messages, TOOL_DEFINITIONS, allow_tools=step < max_steps - 1, usage=usage
            ):
                if event["type"] == "turn":
                    turn = event["result"]
                else:
                    yield event
            assert turn is not None
            if not turn.tool_calls:
                yield {"type": "final", "text": turn.text}
                return
            messages.append({"role": "assistant", "content": turn.content})
            results = []
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
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

    async def _turn(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tool_defs: list[dict[str, Any]],
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

    def _system(self, repo: dict[str, Any], template: str) -> list[dict[str, Any]]:
        paths = self.store.list_paths(repo["id"])
        text = template.format(
            name=repo["name"], commit=(repo.get("commit_sha") or "local")[:10], repo_map=repo_map(paths)
        )
        # Stable per repo, so tools + system prompt are cached across questions.
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    def _history_messages(self, history: list[HistoryTurn]) -> list[dict[str, Any]]:
        """Only previous Q&A text is kept, not tool traffic: cheap, and enough for follow-ups."""
        messages: list[dict[str, Any]] = []
        for turn in history[-self.settings.history_max_turns :]:
            messages.append({"role": "user", "content": turn.question})
            messages.append({"role": "assistant", "content": turn.answer})
        return messages


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
