"""All SQL lives here. Retrieval primitives: vector, keyword, hybrid (RRF), grep."""

import re
from collections.abc import Sequence
from typing import Any

import numpy as np
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.ingest.chunker import split_identifier
from app.models import Chunk, SearchHit, SourceFile

# Tests mention every public name many times, so they crowd out the implementation
# for "how does X work" questions. Halving their score is a prior, measured in evals.
TEST_PATH_SQL = r"path ~ '(^|/)tests?/|(^|/)test_[^/]*$|_test\.py$'"
TEST_PENALTY = 0.5
_TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.py$")
RRF_K = 60  # standard constant from the RRF paper; dampens the impact of top ranks
_STOPWORDS = frozenset(
    """a an and are as at be by can do does for from how i in is it of on or the this to what when
    where which who why with you your there their its me my we our use used using work works
    implement implemented code repo repository function functions file files""".split()
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def keyword_terms(question: str) -> list[str]:
    """Turn a natural-language question into OR-able tsquery terms."""
    terms: list[str] = []
    for word in _WORD.findall(question):
        for term in [word, *split_identifier(word)]:
            term = term.lower()
            if len(term) > 1 and term not in _STOPWORDS and term not in terms:
                terms.append(term)
    return terms


def rrf_fuse(ranked_lists: dict[str, Sequence[SearchHit]], limit: int) -> list[SearchHit]:
    """Reciprocal Rank Fusion: score = sum(1 / (k + rank)) across retrievers."""
    fused: dict[int, SearchHit] = {}
    for source, hits in ranked_lists.items():
        for rank, hit in enumerate(hits, start=1):
            entry = fused.setdefault(hit.chunk_id, SearchHit(**{**hit.__dict__, "score": 0.0, "sources": []}))
            entry.score += 1.0 / (RRF_K + rank)
            entry.sources.append(source)
    return sorted(fused.values(), key=lambda h: h.score, reverse=True)[:limit]


class RepoStore:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self.pool = pool

    # ---- repos -------------------------------------------------------------
    def upsert_repo(self, url: str, name: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO repos (url, name, status) VALUES (%s, %s, 'pending')
                   ON CONFLICT (url) DO UPDATE SET status = 'pending', error = NULL
                   RETURNING id""",
                (url, name),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def set_status(self, repo_id: int, status: str, error: str | None = None, **fields: Any) -> None:
        sets = ["status = %(status)s", "error = %(error)s"] + [f"{k} = %({k})s" for k in fields]
        if status == "ready":
            sets.append("indexed_at = now()")
        with self.pool.connection() as conn:
            conn.execute(
                f"UPDATE repos SET {', '.join(sets)} WHERE id = %(id)s",  # keys are code-defined, not user input
                {"status": status, "error": error, "id": repo_id, **fields},
            )

    def fail_interrupted(self) -> int:
        """Ingestion runs in-process, so any repo still 'busy' at startup was interrupted."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                """UPDATE repos SET status = 'failed', error = 'Indexing was interrupted; add the repo again.'
                   WHERE status IN ('pending', 'cloning', 'parsing', 'embedding')"""
            )
            return cur.rowcount

    def list_repos(self) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM repos ORDER BY created_at DESC").fetchall()

    def get_repo(self, repo_id: int) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM repos WHERE id = %s", (repo_id,)).fetchone()

    def find_repo_by_url(self, url: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM repos WHERE url = %s", (url,)).fetchone()

    def replace_contents(
        self,
        repo_id: int,
        files: Sequence[SourceFile],
        chunks: Sequence[Chunk],
        search_texts: Sequence[str],
        embeddings: Sequence[np.ndarray],
    ) -> None:
        """Swap the repo's index atomically so readers never see a half-built state."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM chunks WHERE repo_id = %s", (repo_id,))
            conn.execute("DELETE FROM files WHERE repo_id = %s", (repo_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO files (repo_id, path, content, line_count) VALUES (%s, %s, %s, %s)",
                    [(repo_id, f.path, f.content, f.line_count) for f in files],
                )
                cur.executemany(
                    """INSERT INTO chunks (repo_id, path, symbol, kind, start_line, end_line,
                                          content, search_text, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (repo_id, c.path, c.symbol, c.kind, c.start_line, c.end_line, c.content, st, emb)
                        for c, st, emb in zip(chunks, search_texts, embeddings, strict=True)
                    ],
                )

    # ---- retrieval -----------------------------------------------------------
    _HIT_COLUMNS = "id AS chunk_id, path, symbol, kind, start_line, end_line, content"

    def vector_search(self, repo_id: int, query_vec: np.ndarray, limit: int) -> list[SearchHit]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {self._HIT_COLUMNS}, 1 - (embedding <=> %s) AS score
                    FROM chunks WHERE repo_id = %s ORDER BY embedding <=> %s LIMIT %s""",
                (query_vec, repo_id, query_vec, limit),
            ).fetchall()
        return [SearchHit(**r, sources=["vector"]) for r in rows]

    def keyword_search(self, repo_id: int, question: str, limit: int) -> list[SearchHit]:
        terms = keyword_terms(question)
        if not terms:
            return []
        tsquery = " | ".join(terms)  # terms are [a-z0-9_] only, safe for to_tsquery
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {self._HIT_COLUMNS},
                           ts_rank_cd(tsv, q, 1) * CASE WHEN {TEST_PATH_SQL} THEN %s ELSE 1 END AS score
                    FROM chunks, to_tsquery('simple', %s) q
                    WHERE repo_id = %s AND tsv @@ q
                    ORDER BY score DESC LIMIT %s""",
                (TEST_PENALTY, tsquery, repo_id, limit),
            ).fetchall()
        return [SearchHit(**r, sources=["keyword"]) for r in rows]

    def hybrid_search(self, repo_id: int, question: str, query_vec: np.ndarray, limit: int) -> list[SearchHit]:
        pool_size = max(limit * 4, 30)
        fused = rrf_fuse(
            {
                "vector": self.vector_search(repo_id, query_vec, pool_size),
                "keyword": self.keyword_search(repo_id, question, pool_size),
            },
            limit=pool_size,
        )
        for hit in fused:
            if _TEST_PATH.search(hit.path):
                hit.score *= TEST_PENALTY
        return sorted(fused, key=lambda h: h.score, reverse=True)[:limit]

    # ---- file access (agent tools, code viewer) ------------------------------
    def list_paths(self, repo_id: int, prefix: str = "") -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT path FROM files WHERE repo_id = %s AND path LIKE %s ORDER BY path",
                (repo_id, _like_prefix(prefix)),
            ).fetchall()
        return [r["path"] for r in rows]

    def get_file(self, repo_id: int, path: str) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT content FROM files WHERE repo_id = %s AND path = %s", (repo_id, path)).fetchone()
        return row["content"] if row else None

    def file_line_counts(self, repo_id: int) -> dict[str, int]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT path, line_count FROM files WHERE repo_id = %s", (repo_id,)).fetchall()
        return {r["path"]: r["line_count"] for r in rows}

    def grep(self, repo_id: int, pattern: str, prefix: str = "", max_matches: int = 50) -> list[tuple[str, int, str]]:
        """Literal, case-sensitive substring search. Returns (path, line_no, line)."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT path, content FROM files
                   WHERE repo_id = %s AND path LIKE %s AND strpos(content, %s) > 0 ORDER BY path""",
                (repo_id, _like_prefix(prefix), pattern),
            ).fetchall()
        matches = []
        for row in rows:
            for no, line in enumerate(row["content"].splitlines(), start=1):
                if pattern in line:
                    matches.append((row["path"], no, line.strip()))
                    if len(matches) >= max_matches:
                        return matches
        return matches

    # ---- observability -----------------------------------------------------
    def log_query(self, **row: Any) -> None:
        row["steps"] = Jsonb(row.get("steps", []))
        cols = ", ".join(row)
        vals = ", ".join(f"%({k})s" for k in row)
        with self.pool.connection() as conn:
            conn.execute(f"INSERT INTO query_logs ({cols}) VALUES ({vals})", row)


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.lstrip("/") + "%"
