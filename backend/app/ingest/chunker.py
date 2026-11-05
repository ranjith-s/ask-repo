"""Split source files into retrieval units.

Python is split along its syntax tree, so each chunk is a whole function, method
or class outline rather than an arbitrary window of lines. Everything else
(Markdown, config files) falls back to heading- or line-based windows.
"""

import ast
import re

from app.models import Chunk, SourceFile

MAX_CHUNK_LINES = 120  # longer definitions are split into overlapping windows
WINDOW_LINES = 60
WINDOW_OVERLAP = 10


def chunk_file(file: SourceFile) -> list[Chunk]:
    from app.ingest.script_chunker import ScriptChunker, language_for  # tree-sitter loads lazily

    if (language := language_for(file.path)) is not None:
        try:
            return ScriptChunker(file, language).chunks()
        except (ValueError, RecursionError):
            return _window_chunks(file, kind="text", symbol=file.path)
    if file.path.endswith(".py"):
        try:
            return PythonChunker(file).chunks()
        except SyntaxError:
            pass  # e.g. Python 2 files or templates; still worth indexing as text
    if file.path.endswith((".md", ".rst")):
        return _markdown_chunks(file)
    return _window_chunks(file, kind="text", symbol=file.path)


class PythonChunker:
    def __init__(self, file: SourceFile) -> None:
        self.file = file
        self.lines = file.content.splitlines()
        self.tree = ast.parse(file.content)
        self.module = file.path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")

    def chunks(self) -> list[Chunk]:
        out = [self._module_outline()]
        loose: list[ast.stmt] = []  # top-level statements that are not definitions
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                out.extend(self._definition(node, node.name, "function"))
            elif isinstance(node, ast.ClassDef):
                out.extend(self._class(node))
            else:
                loose.append(node)
        out.extend(self._loose_code(loose))
        return out

    def _segment(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start - 1 : end])

    @staticmethod
    def _start(node: ast.AST) -> int:
        decorators = getattr(node, "decorator_list", [])
        return min([d.lineno for d in decorators] + [node.lineno])  # type: ignore[attr-defined]

    def _signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
        body_start = node.body[0].lineno if node.body else node.lineno
        header = self._segment(node.lineno, max(node.lineno, body_start - 1)).strip()
        return header.rstrip(":").strip() or f"def {node.name}(...)"

    def _module_outline(self) -> Chunk:
        """A synthetic chunk that answers 'what is in this file?'."""
        doc = ast.get_docstring(self.tree) or ""
        imports = [
            self._segment(n.lineno, n.end_lineno or n.lineno)
            for n in self.tree.body
            if isinstance(n, ast.Import | ast.ImportFrom)
        ]
        defs = [
            self._signature(n)
            for n in self.tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        ]
        parts = [f"# module {self.module}"]
        if doc:
            parts.append(f'"""{doc}"""')
        if imports:
            parts.append("\n".join(imports))
        if defs:
            parts.append("# defines:\n" + "\n".join(defs))
        return Chunk(self.file.path, self.module, "module", 1, max(1, len(self.lines)), "\n\n".join(parts))

    def _class(self, node: ast.ClassDef) -> list[Chunk]:
        methods = [n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        header = self._signature(node)
        doc = ast.get_docstring(node)
        outline = [header + ":"]
        if doc:
            outline.append(f'    """{doc}"""')
        # Class attributes and assignments (e.g. dataclass fields) belong in the outline.
        for stmt in node.body:
            if isinstance(stmt, ast.Assign | ast.AnnAssign):
                outline.append("    " + self._segment(stmt.lineno, stmt.end_lineno or stmt.lineno).strip())
        outline.extend("    " + self._signature(m) for m in methods)
        start, end = self._start(node), node.end_lineno or node.lineno
        out = [Chunk(self.file.path, node.name, "class", start, end, "\n".join(outline))]
        for method in methods:
            out.extend(self._definition(method, f"{node.name}.{method.name}", "method", context=header))
        return out

    def _definition(self, node: ast.AST, symbol: str, kind: str, context: str | None = None) -> list[Chunk]:
        start, end = self._start(node), node.end_lineno or node.lineno  # type: ignore[attr-defined]
        prefix = f"# in {context}\n" if context else ""
        if end - start + 1 <= MAX_CHUNK_LINES:
            return [Chunk(self.file.path, symbol, kind, start, end, prefix + self._segment(start, end))]
        return [
            Chunk(self.file.path, symbol, kind, s, e, prefix + self._segment(s, e))
            for s, e in _windows(start, end, MAX_CHUNK_LINES, WINDOW_OVERLAP)
        ]

    def _loose_code(self, nodes: list[ast.stmt]) -> list[Chunk]:
        """Group consecutive non-definition statements (constants, __all__, etc.)."""
        groups: list[list[ast.stmt]] = []
        for node in nodes:
            if isinstance(node, ast.Import | ast.ImportFrom | ast.Expr) and not groups:
                continue  # imports and the docstring are already in the outline
            if groups and node.lineno <= (groups[-1][-1].end_lineno or 0) + 2:
                groups[-1].append(node)
            else:
                groups.append([node])
        out = []
        for group in groups:
            start, end = group[0].lineno, group[-1].end_lineno or group[-1].lineno
            if end - start < 1 and len(self.lines[start - 1]) < 40:
                continue  # skip trivial one-liners
            out.append(Chunk(self.file.path, f"@{start}", "code", start, end, self._segment(start, end)))
        return out


_HEADING = re.compile(r"^#{1,4}\s+(.*)")


def _markdown_chunks(file: SourceFile) -> list[Chunk]:
    lines = file.content.splitlines()
    starts = [i for i, line in enumerate(lines) if _HEADING.match(line)]
    if not starts:
        return _window_chunks(file, kind="doc", symbol=file.path)
    if starts[0] != 0:
        starts.insert(0, 0)
    out = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue
        heading = _HEADING.match(lines[start])
        title = heading.group(1).strip() if heading else file.path
        for s, e in _windows(start + 1, end, WINDOW_LINES * 2, WINDOW_OVERLAP):
            out.append(Chunk(file.path, title, "doc", s, e, "\n".join(lines[s - 1 : e])))
    return out


def _window_chunks(file: SourceFile, kind: str, symbol: str) -> list[Chunk]:
    lines = file.content.splitlines()
    return [
        Chunk(file.path, symbol, kind, s, e, "\n".join(lines[s - 1 : e]))
        for s, e in _windows(1, max(1, len(lines)), WINDOW_LINES, WINDOW_OVERLAP)
    ]


def _windows(start: int, end: int, size: int, overlap: int) -> list[tuple[int, int]]:
    """Inclusive 1-based line windows covering [start, end]."""
    if end - start + 1 <= size:
        return [(start, end)]
    out, s = [], start
    while s <= end:
        e = min(end, s + size - 1)
        out.append((s, e))
        if e == end:
            break
        s = e - overlap + 1
    return out


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PUNCT = re.compile(r"[./:\-]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def split_identifier(word: str) -> list[str]:
    """`url_path_for` -> [url, path, for]; `HTTPEndpoint` -> [HTTPEndpoint]; `getUser` -> [get, User]."""
    parts = [p for chunk in word.split("_") for p in _CAMEL.split(chunk) if p]
    return parts if len(parts) > 1 else []


def search_text(chunk: Chunk) -> str:
    """Text fed to the keyword index: path, symbol and code, plus split identifiers
    so a question about 'url path' still matches `url_path_for`."""
    idents = set(_IDENT.findall(chunk.symbol + " " + chunk.content))
    extra = {part.lower() for ident in idents for part in split_identifier(ident)}
    # Postgres' parser reads `routing.py` or `a.b-c` as single file/host tokens, which
    # then never match a question. Breaking on punctuation keeps the words searchable.
    text = _PUNCT.sub(" ", " ".join([chunk.path, chunk.symbol, chunk.content]))
    return " ".join([text, *sorted(extra)])


def symbol_terms(chunk: Chunk) -> str:
    """The words of a chunk's name, for the highest keyword weight.

    camelCase is split here because Postgres only splits snake_case by itself: without this,
    `normalizeRequestMethod` would be one opaque token while `normalize_request_method` gets
    three useful words. Loose code (named by line) and tests get no name weight: a test title
    like "retries without the computed delay" describes a check, not where the code lives.
    """
    from app.ingest.source import is_test_path

    if chunk.kind == "code" or is_test_path(chunk.path):
        return ""
    words = [w for w in re.split(r"[^A-Za-z0-9_]+", chunk.symbol) if w]
    parts = {p.lower() for w in words for p in split_identifier(w)}
    return " ".join([*words, *sorted(parts)])


def embedding_text(chunk: Chunk, max_chars: int = 2000) -> str:
    return f"{chunk.path}\n{chunk.kind} {chunk.symbol}\n{chunk.content}"[:max_chars]
