"""Parse [path:Lx-Ly] citations from an answer and check them against the repo.

valid    = the file exists and the line range is inside it
grounded = the model was actually shown those lines during this answer
"""

import re
from dataclasses import asdict, dataclass

from app.agent.tools import SeenLines

# Paths may contain '@' (monorepo packages such as packages/@scope/name/src/index.ts).
CITATION = re.compile(r"\[([\w.@/-]+):L?(\d+)(?:\s*-\s*L?(\d+))?\]")


@dataclass
class Citation:
    path: str
    start: int
    end: int
    valid: bool
    grounded: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def check_citations(answer: str, line_counts: dict[str, int], seen: SeenLines) -> list[Citation]:
    out: dict[tuple[str, int, int], Citation] = {}
    for match in CITATION.finditer(answer):
        path, start = match[1], int(match[2])
        end = int(match[3]) if match[3] else start
        start, end = min(start, end), max(start, end)
        total = line_counts.get(path)
        valid = total is not None and start >= 1 and end <= total
        out[(path, start, end)] = Citation(path, start, end, valid, valid and seen.overlaps(path, start, end))
    return list(out.values())
