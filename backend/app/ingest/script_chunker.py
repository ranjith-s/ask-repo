"""Split JavaScript and TypeScript along their syntax tree, using tree-sitter.

Produces the same kinds of chunks as the Python chunker, so retrieval and the
agent treat both alike:

- module   a synthetic outline: imports/requires plus every top-level signature
- function function declarations, `const f = () => …`, and assignments such as
           `res.send = function send() {…}` (how CommonJS libraries like Express
           define their API)
- class    a synthetic outline: header, fields and method signatures
- method   each class method, prefixed with its class header for context
- type     interfaces, type aliases and enums (TypeScript)
- code     everything else at the top level, grouped (e.g. route registrations)

A JSDoc comment directly above a definition is included in its chunk: in JS/TS
the explanation sits above the code, unlike Python docstrings.
"""

import re
from functools import cache

import tree_sitter as ts

from app.models import Chunk, SourceFile

SCRIPT_LANGUAGES = {
    ".js": "javascript",
    ".jsx": "javascript",  # the JavaScript grammar includes JSX
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",  # TypeScript's grammar rejects JSX, so .tsx has its own
}

FUNCTION_DECLS = {"function_declaration", "generator_function_declaration"}
CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
TYPE_DECLS = {"interface_declaration", "type_alias_declaration", "enum_declaration"}
VARIABLE_DECLS = {"lexical_declaration", "variable_declaration"}
FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function", "class"}
METHODS = {"method_definition"}
METHOD_SIGNATURES = {"method_signature", "abstract_method_signature"}
FIELDS = {"public_field_definition", "field_definition"}
_SPACES = re.compile(r"\s+")


@cache
def _language(name: str) -> ts.Language:
    if name == "javascript":
        import tree_sitter_javascript as grammar

        return ts.Language(grammar.language())
    import tree_sitter_typescript as grammar_ts

    return ts.Language(grammar_ts.language_tsx() if name == "tsx" else grammar_ts.language_typescript())


def language_for(path: str) -> str | None:
    for suffix, name in SCRIPT_LANGUAGES.items():
        if path.endswith(suffix):
            return name
    return None


class ScriptChunker:
    def __init__(self, file: SourceFile, language: str) -> None:
        self.file = file
        self.source = file.content.encode("utf-8")
        self.lines = file.content.splitlines()
        # A parser per file: parsers are cheap, and ingestion may run in several threads.
        self.root = ts.Parser(_language(language)).parse(self.source).root_node
        self.module = re.sub(r"\.[cm]?[jt]sx?$", "", file.path).replace("/", ".")

    def chunks(self) -> list[Chunk]:
        from app.ingest.chunker import MAX_CHUNK_LINES, WINDOW_OVERLAP, _windows

        self._windows, self._max, self._overlap = _windows, MAX_CHUNK_LINES, WINDOW_OVERLAP
        definitions: list[Chunk] = []
        imports: list[str] = []
        signatures: list[str] = []
        loose: list[ts.Node] = []
        for node, start_row in self._with_leading_comments(self.root.named_children):
            if self._is_import(node):
                imports.append(self._text(node).strip())
                continue
            chunks = self._definition(node, start_row)
            if chunks:
                definitions.extend(chunks)
                signatures.append(self._signature(node))
            else:
                loose.append(node)
        return [self._outline(imports, signatures), *definitions, *self._loose_code(loose)]

    # ---- helpers ------------------------------------------------------------------
    def _text(self, node: ts.Node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def _segment(self, start: int, end: int) -> str:
        """1-based, inclusive line range."""
        return "\n".join(self.lines[start - 1 : end])

    @staticmethod
    def _with_leading_comments(nodes: list[ts.Node]) -> list[tuple[ts.Node, int]]:
        """Pair each non-comment node with the row its chunk should start at: the first row of
        a comment block that ends on the line right above it (JSDoc), else its own row."""
        out: list[tuple[ts.Node, int]] = []
        comment_start: int | None = None
        previous_end = -2
        for node in nodes:
            if node.type == "comment":
                adjacent = comment_start is not None and node.start_point[0] <= previous_end + 1
                comment_start = comment_start if adjacent else node.start_point[0]
                previous_end = node.end_point[0]
                continue
            touching = comment_start is not None and node.start_point[0] <= previous_end + 1
            out.append((node, comment_start if touching and comment_start is not None else node.start_point[0]))
            comment_start, previous_end = None, node.end_point[0]
        return out

    def _is_import(self, node: ts.Node) -> bool:
        if node.type == "import_statement":
            return True
        if node.type in VARIABLE_DECLS:  # const x = require('x')
            values = [d.child_by_field_name("value") for d in node.named_children]
            return bool(values) and all(
                v is not None and v.type == "call_expression" and self._text(v).startswith("require(") for v in values
            )
        return False

    @staticmethod
    def _unwrap(node: ts.Node) -> ts.Node:
        """`export …` and `export default …` wrap the declaration we care about."""
        if node.type == "export_statement":
            inner = node.child_by_field_name("declaration") or node.child_by_field_name("value")
            if inner is not None:
                return inner
        return node

    def _signature(self, node: ts.Node) -> str:
        """Everything before the body, on one line: `export async function load(id: string): Promise<T>`."""
        decl = self._unwrap(node)
        body = decl.child_by_field_name("body")
        if body is None and decl.type in VARIABLE_DECLS:
            value = next((d.child_by_field_name("value") for d in decl.named_children), None)
            body = value.child_by_field_name("body") if value is not None else None
        if body is None and decl.type == "expression_statement":
            body = self._function_in(decl)
            body = body.child_by_field_name("body") if body is not None else None
        end = body.start_byte if body is not None and body.type in ("statement_block", "class_body") else None
        text = self.source[node.start_byte : end].decode("utf-8", errors="replace") if end else self._text(node)
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("@")]  # drop decorators
        return _SPACES.sub(" ", " ".join(lines)).strip().rstrip("{").strip()[:200]

    def _function_in(self, statement: ts.Node) -> ts.Node | None:
        """The function an expression statement defines, if any."""
        expr = statement.named_children[0] if statement.named_children else None
        if expr is None:
            return None
        if expr.type == "assignment_expression":
            right = expr.child_by_field_name("right")
            while right is not None and right.type == "assignment_expression":  # a.get = a.header = function…
                right = right.child_by_field_name("right")
            return right if right is not None and right.type in FUNCTION_VALUES else None
        if expr.type == "call_expression":  # defineGetter(req, 'ip', function ip() {…}), router.post('/x', fn)
            args = expr.child_by_field_name("arguments")
            for arg in args.named_children if args is not None else []:
                if arg.type in FUNCTION_VALUES:
                    return arg
        return None

    def _call_symbol(self, call: ts.Node, fn: ts.Node) -> str:
        """Name a call that passes a function: the function's own name if it has one, else the
        callee plus its first string argument, e.g. `router.post /login` or `defineGetter fresh`."""
        fn_name = fn.child_by_field_name("name")
        if fn_name is not None:
            return self._text(fn_name)
        callee = call.child_by_field_name("function")
        label = _SPACES.sub("", self._text(callee))[:60] if callee is not None else "call"
        args = call.child_by_field_name("arguments")
        first_string = next((a for a in (args.named_children if args else []) if a.type == "string"), None)
        return f"{label} {self._text(first_string).strip(chr(39) + chr(34))}"[:80] if first_string else label

    # ---- definitions --------------------------------------------------------------
    def _definition(self, node: ts.Node, start_row: int) -> list[Chunk] | None:
        decl = self._unwrap(node)
        start, end = start_row + 1, node.end_point[0] + 1
        name_node = decl.child_by_field_name("name")
        name = self._text(name_node) if name_node is not None else None

        if decl.type in FUNCTION_DECLS or (decl.type in FUNCTION_VALUES and node.type == "export_statement"):
            return self._spans(name or "default", "function", start, end)
        if decl.type in CLASS_DECLS or (decl.type == "class" and node.type == "export_statement"):
            return self._class(decl, name or "default", start, end)
        if decl.type in TYPE_DECLS and name:
            return self._spans(name, "type", start, end)
        if decl.type in VARIABLE_DECLS:
            names = [
                self._text(n)
                for d in decl.named_children
                if (v := d.child_by_field_name("value")) is not None
                and v.type in FUNCTION_VALUES
                and (n := d.child_by_field_name("name")) is not None
            ]
            return self._spans(names[0], "function", start, end) if names else None
        if decl.type == "expression_statement" and (fn := self._function_in(decl)) is not None:
            expr = decl.named_children[0]
            if expr.type == "assignment_expression":
                left = expr.child_by_field_name("left")
                symbol = _SPACES.sub("", self._text(left))[:80] if left is not None else "anonymous"
            else:
                symbol = self._call_symbol(expr, fn)
            return self._spans(symbol, "function", start, end)
        return None

    def _spans(self, symbol: str, kind: str, start: int, end: int, prefix: str = "") -> list[Chunk]:
        return [
            Chunk(self.file.path, symbol, kind, s, e, prefix + self._segment(s, e))
            for s, e in self._windows(start, end, self._max, self._overlap)
        ]

    def _class(self, decl: ts.Node, name: str, start: int, end: int) -> list[Chunk]:
        body = decl.child_by_field_name("body")
        header_end = body.start_byte if body is not None else decl.end_byte
        header_text = self.source[decl.start_byte : header_end].decode("utf-8", errors="replace")
        header = _SPACES.sub(" ", header_text).strip()
        members = body.named_children if body is not None else []

        outline = [f"{header} {{"]
        methods: list[Chunk] = []
        for member, member_start in self._with_leading_comments(members):
            if member.type in FIELDS:
                outline.append("  " + _SPACES.sub(" ", self._text(member)).strip()[:160])
            elif member.type in METHODS | METHOD_SIGNATURES:
                member_body = member.child_by_field_name("body")
                cut = member_body.start_byte if member_body is not None else member.end_byte
                sig = self.source[member.start_byte : cut].decode("utf-8", errors="replace")
                outline.append("  " + _SPACES.sub(" ", sig).strip().rstrip(";")[:200])
                member_name = member.child_by_field_name("name")
                if member.type in METHODS and member_name is not None:
                    methods.extend(
                        self._spans(
                            f"{name}.{self._text(member_name)}",
                            "method",
                            member_start + 1,
                            member.end_point[0] + 1,
                            prefix=f"// in {header}\n",
                        )
                    )
        outline.append("}")
        return [Chunk(self.file.path, name, "class", start, end, "\n".join(outline)), *methods]

    # ---- outline and leftovers --------------------------------------------------------
    def _outline(self, imports: list[str], signatures: list[str]) -> Chunk:
        parts = [f"// module {self.module}"]
        if imports:
            parts.append("\n".join(imports))
        if signatures:
            parts.append("// defines:\n" + "\n".join(signatures))
        return Chunk(self.file.path, self.module, "module", 1, max(1, len(self.lines)), "\n\n".join(parts))

    def _loose_code(self, nodes: list[ts.Node]) -> list[Chunk]:
        """Group consecutive top-level statements (route registrations, config objects, …)."""
        groups: list[list[ts.Node]] = []
        for node in nodes:
            if node.type == "expression_statement" and self._text(node).strip(" ;") in ("'use strict'", '"use strict"'):
                continue
            if groups and node.start_point[0] <= groups[-1][-1].end_point[0] + 2:
                groups[-1].append(node)
            else:
                groups.append([node])
        out: list[Chunk] = []
        for group in groups:
            start, end = group[0].start_point[0] + 1, group[-1].end_point[0] + 1
            out.extend(self._spans(f"@{start}", "code", start, end))
        return out
