"""Plain domain objects shared across ingestion, search and the agent."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: str

    @property
    def line_count(self) -> int:
        return self.content.count("\n") + 1


@dataclass(frozen=True)
class Chunk:
    path: str
    symbol: str
    kind: str  # module | class | function | method | code | doc | text
    start_line: int
    end_line: int
    content: str


@dataclass
class SearchHit:
    chunk_id: int
    path: str
    symbol: str
    kind: str
    start_line: int
    end_line: int
    content: str
    score: float = 0.0
    sources: list[str] = field(default_factory=list)  # which retrievers found it
