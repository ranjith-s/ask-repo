from app.ingest.chunker import PythonChunker, _windows, chunk_file, search_text, split_identifier
from app.models import SourceFile

SOURCE = '''"""Module doc."""
import os

LIMIT = 10


@decorator
def top(a, b):
    """Top-level function."""
    return a + b


class Widget(Base):
    """A widget."""

    size: int = 3

    def render(self):
        return "<w>"

    async def fetch(self, url):
        return url
'''


def by_symbol(chunks):
    return {c.symbol: c for c in chunks}


def test_python_is_split_along_definitions():
    chunks = by_symbol(chunk_file(SourceFile("pkg/widgets.py", SOURCE)))
    assert {"pkg.widgets", "top", "Widget", "Widget.render", "Widget.fetch"} <= chunks.keys()
    assert chunks["top"].kind == "function"
    assert chunks["Widget.render"].kind == "method"


def test_line_numbers_include_decorators_and_are_1_based():
    chunks = by_symbol(chunk_file(SourceFile("pkg/widgets.py", SOURCE)))
    top = chunks["top"]
    assert SOURCE.splitlines()[top.start_line - 1] == "@decorator"
    assert SOURCE.splitlines()[top.end_line - 1].strip() == "return a + b"


def test_methods_carry_their_class_as_context():
    method = by_symbol(chunk_file(SourceFile("pkg/widgets.py", SOURCE)))["Widget.fetch"]
    assert method.content.startswith("# in class Widget(Base)")


def test_class_outline_lists_fields_and_method_signatures():
    outline = by_symbol(chunk_file(SourceFile("pkg/widgets.py", SOURCE)))["Widget"].content
    assert "size: int = 3" in outline
    assert "def render(self)" in outline and "async def fetch(self, url)" in outline
    assert 'return "<w>"' not in outline  # bodies live in the method chunks


def test_module_outline_has_doc_imports_and_definitions():
    module = by_symbol(chunk_file(SourceFile("pkg/widgets.py", SOURCE)))["pkg.widgets"]
    assert "Module doc." in module.content and "import os" in module.content
    assert "def top(a, b)" in module.content


def test_init_module_name_drops_init():
    assert PythonChunker(SourceFile("pkg/__init__.py", "x = 1\n")).module == "pkg"


def test_long_functions_are_windowed_with_overlap():
    body = "\n".join(f"    x{i} = {i}" for i in range(300))
    chunks = [c for c in chunk_file(SourceFile("big.py", f"def big():\n{body}\n")) if c.symbol == "big"]
    assert len(chunks) > 1
    assert chunks[1].start_line < chunks[0].end_line  # windows overlap
    assert chunks[-1].end_line == 301


def test_invalid_python_falls_back_to_text_windows():
    chunks = chunk_file(SourceFile("legacy.py", "print 'hello'\n"))
    assert [c.kind for c in chunks] == ["text"]


def test_markdown_split_by_headings():
    md = "# Title\nintro\n\n## Install\npip install x\n\n## Usage\nrun it\n"
    chunks = chunk_file(SourceFile("README.md", md))
    assert [c.symbol for c in chunks] == ["Title", "Install", "Usage"]


def test_windows_cover_range_exactly():
    assert _windows(1, 10, 60, 10) == [(1, 10)]
    wins = _windows(1, 130, 60, 10)
    assert wins[0] == (1, 60) and wins[-1][1] == 130


def test_identifier_splitting_helps_keyword_search():
    assert split_identifier("url_path_for") == ["url", "path", "for"]
    assert split_identifier("getUserName") == ["get", "User", "Name"]
    assert split_identifier("simple") == []
    chunk = by_symbol(chunk_file(SourceFile("r.py", "def url_path_for(): pass\n")))["url_path_for"]
    assert "path" in search_text(chunk).split()
