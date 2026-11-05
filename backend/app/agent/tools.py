"""The tools the agent can call, and what each one is allowed to touch.

Every tool is read-only and scoped to one indexed repo. File paths come from
the model, so they are only ever looked up in the `files` table, never on disk.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.agent.llm.base import ToolSpec
from app.embeddings import Embedder
from app.models import SearchHit
from app.store import RepoStore

READ_FILE_MAX_LINES = 250
LIST_FILES_MAX = 200
SNIPPET_LINES = 12
# Outlines are synthesized summaries, so seeing one is not evidence for the lines it spans.
SYNTHETIC_KINDS = {"module", "class"}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="search_code",
        description=(
            "Hybrid semantic + keyword search over the repository's functions, classes, "
            "module outlines and docs. Use natural language or identifiers. Returns ranked "
            "locations with a short snippet; call read_file to see more."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look for"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="grep",
        description=(
            "Exact, case-sensitive substring search across all indexed files. Best for "
            "finding where an identifier is defined or used, e.g. 'def url_path_for' or 'CORSMiddleware('."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Literal text to find (min 2 chars)"},
                "path_prefix": {"type": "string", "description": "Limit to paths starting with this; '' for all"},
            },
            "required": ["pattern", "path_prefix"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="read_file",
        description=f"Read a line range of a file (1-based, inclusive, max {READ_FILE_MAX_LINES} lines).",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path", "start_line", "end_line"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="list_files",
        description="List indexed file paths under a directory prefix ('' for the whole repo).",
        parameters={
            "type": "object",
            "properties": {"path_prefix": {"type": "string"}},
            "required": ["path_prefix"],
            "additionalProperties": False,
        },
    ),
]


@dataclass
class ToolOutcome:
    content: str
    summary: str  # one line for the UI's step list
    is_error: bool = False
    locations: list[dict[str, Any]] = field(default_factory=list)


class SeenLines:
    """Tracks which lines the model has been shown, so citations can be checked against evidence."""

    def __init__(self) -> None:
        self._ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)

    def add(self, path: str, start: int, end: int) -> None:
        self._ranges[path].append((start, end))

    def overlaps(self, path: str, start: int, end: int) -> bool:
        return any(s <= end and start <= e for s, e in self._ranges.get(path, []))


class RepoTools:
    def __init__(self, store: RepoStore, embedder: Embedder, repo_id: int, max_chars: int) -> None:
        self.store = store
        self.embedder = embedder
        self.repo_id = repo_id
        self.max_chars = max_chars
        self.seen = SeenLines()

    def search(self, query: str, limit: int = 8, shown_lines: int | None = None) -> list[SearchHit]:
        """Hybrid search; marks as seen only the lines the model will actually be shown."""
        vec = self.embedder.embed_query(query)
        hits = self.store.hybrid_search(self.repo_id, query, vec, limit)
        for h in hits:
            if h.kind not in SYNTHETIC_KINDS:
                end = h.end_line if shown_lines is None else min(h.end_line, h.start_line + shown_lines - 1)
                self.seen.add(h.path, h.start_line, end)
        return hits

    async def run(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolOutcome(f"Unknown tool: {name}", f"Tried an unknown action ({name})", is_error=True)
        try:
            outcome: ToolOutcome = await asyncio.to_thread(handler, **args)
        except (TypeError, ValueError) as exc:
            return ToolOutcome(f"Invalid arguments: {exc}", "A step failed because of invalid input", is_error=True)
        if len(outcome.content) > self.max_chars:
            outcome.content = outcome.content[: self.max_chars] + "\n…[truncated; narrow the request]"
        return outcome

    # Each handler is sync (DB + CPU work) and runs in a worker thread.
    def _tool_search_code(self, query: str) -> ToolOutcome:
        hits = self.search(query, shown_lines=SNIPPET_LINES)
        if not hits:
            return ToolOutcome("No results.", f'Searched for "{query}": nothing found')
        blocks = []
        for h in hits:
            snippet = "\n".join(h.content.splitlines()[:SNIPPET_LINES])
            blocks.append(f"## {h.path}:L{h.start_line}-L{h.end_line} ({h.kind} {h.symbol})\n{snippet}")
        locations = [{"path": h.path, "start": h.start_line, "end": h.end_line} for h in hits]
        return ToolOutcome(
            "\n\n".join(blocks), f'Searched for "{query}": {_count(len(hits), "place")}', locations=locations
        )

    def _tool_grep(self, pattern: str, path_prefix: str = "") -> ToolOutcome:
        if len(pattern) < 2 or len(pattern) > 200:
            raise ValueError("pattern must be 2-200 characters")
        matches = self.store.grep(self.repo_id, pattern, path_prefix)
        for path, line, _ in matches:
            self.seen.add(path, line, line)
        body = "\n".join(f"{p}:L{n}: {text}" for p, n, text in matches) or "No matches."
        return ToolOutcome(body, f'Looked for the exact text "{pattern}": {_count(len(matches), "match", "matches")}')

    def _tool_read_file(self, path: str, start_line: int, end_line: int) -> ToolOutcome:
        content = self.store.get_file(self.repo_id, path)
        if content is None:
            return ToolOutcome(
                f"File not found: {path}. Use list_files to see valid paths.",
                f"Tried to open {path}: no such file",
                is_error=True,
            )
        lines = content.splitlines()
        if start_line > len(lines):
            return ToolOutcome(
                f"{path} has only {len(lines)} lines.", f"Tried to read past the end of {path}", is_error=True
            )
        start = max(1, start_line)
        end = min(len(lines), max(start, end_line), start + READ_FILE_MAX_LINES - 1)
        self.seen.add(path, start, end)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        note = f"\n[file has {len(lines)} lines]" if end < len(lines) else ""
        return ToolOutcome(
            body + note, f"Read {path}, lines {start}–{end}", locations=[{"path": path, "start": start, "end": end}]
        )

    def _tool_list_files(self, path_prefix: str = "") -> ToolOutcome:
        paths = self.store.list_paths(self.repo_id, path_prefix)
        shown = paths[:LIST_FILES_MAX]
        more = f"\n…and {len(paths) - len(shown)} more" if len(paths) > len(shown) else ""
        return ToolOutcome(
            "\n".join(shown) + more or "No files.",
            f"Listed the files in {path_prefix or 'the project'}: {_count(len(paths), 'file')}",
        )


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def repo_map(paths: list[str], max_lines: int = 60) -> str:
    """Compact directory overview for the system prompt: dirs (depth ≤ 2) with file counts."""
    counts: dict[str, int] = defaultdict(int)
    root_files = []
    for p in paths:
        parts = p.split("/")
        if len(parts) == 1:
            root_files.append(p)
        else:
            counts[parts[0] + "/"] += 1
            if len(parts) > 2:
                counts["/".join(parts[:2]) + "/"] += 1
    lines = [*(f"{d} ({n} files)" for d, n in sorted(counts.items())), *root_files]
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], f"… ({len(lines) - max_lines} more entries)"]
    return "\n".join(lines)
