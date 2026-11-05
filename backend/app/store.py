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
from app.ingest.source import TEST_PATH_PATTERN, is_test_path
from app.models import Chunk, SearchHit, SourceFile

# Tests mention every public name many times, so they crowd out the implementation
# for "how does X work" questions. Halving their score is a prior, measured in evals.
TEST_PENALTY = 0.5
# A project in one of these states is being set up by a background task.
BUSY_STATUSES = ("pending", "cloning", "parsing", "embedding")
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
                   ON CONFLICT (url) DO UPDATE SET status = 'pending', error = NULL, cancel_requested = false,
                       progress_done = 0, progress_total = NULL, progress_detail = NULL
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

    def start_phase(self, repo_id: int, status: str, total: int | None, **fields: Any) -> None:
        """Enter an indexing phase and reset its progress. The start time uses the DB clock,
        like the elapsed time reported by list_repos, so clock skew can't distort estimates."""
        sets = ", ".join(f"{k} = %({k})s" for k in fields)  # keys are code-defined, not user input
        with self.pool.connection() as conn:
            conn.execute(
                f"""UPDATE repos SET status = %(status)s, error = NULL, progress_done = 0,
                                     progress_total = %(total)s, progress_detail = NULL,
                                     phase_started_at = now(){", " + sets if sets else ""}
                    WHERE id = %(id)s""",
                {"status": status, "total": total, "id": repo_id, **fields},
            )

    def set_progress(self, repo_id: int, done: int, total: int | None, detail: str | None = None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE repos SET progress_done = %s, progress_total = %s, progress_detail = %s WHERE id = %s",
                (done, total, detail, repo_id),
            )

    def fail_interrupted(self) -> int:
        """Ingestion runs in-process, so any repo still 'busy' at startup was interrupted."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                """UPDATE repos SET status = 'failed', error = %s, cancel_requested = false
                   WHERE status = ANY(%s)""",
                (
                    "Setup was interrupted because the server restarted. Please add the project again.",
                    list(BUSY_STATUSES),
                ),
            )
            return cur.rowcount

    # ---- cancelling and removing ------------------------------------------------------
    def request_cancel(self, repo_id: int) -> bool:
        """Ask the background task to stop. False if the project isn't being set up."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "UPDATE repos SET cancel_requested = true WHERE id = %s AND status = ANY(%s) RETURNING id",
                (repo_id, list(BUSY_STATUSES)),
            ).fetchone()
        return row is not None

    def is_cancel_requested(self, repo_id: int) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT cancel_requested FROM repos WHERE id = %s", (repo_id,)).fetchone()
        return row is None or bool(row["cancel_requested"])  # a removed project counts as cancelled

    def finish_cancel(self, repo_id: int) -> str:
        """Undo a cancelled setup. A project with an earlier index (it was being re-added) goes
        back to it, untouched because the index is only replaced at the very end; a new project
        is removed. Returns "restored" or "removed"."""
        with self.pool.connection() as conn, conn.transaction():
            counts = conn.execute(
                """SELECT (SELECT count(*) FROM files WHERE repo_id = %(id)s) AS files,
                          (SELECT count(*) FROM chunks WHERE repo_id = %(id)s) AS chunks""",
                {"id": repo_id},
            ).fetchone()
            assert counts is not None
            if counts["files"] == 0:
                conn.execute("DELETE FROM repos WHERE id = %s", (repo_id,))
                return "removed"
            conn.execute(
                """UPDATE repos SET status = 'ready', error = NULL, cancel_requested = false,
                                     file_count = %s, chunk_count = %s, progress_done = 0,
                                     progress_total = NULL, progress_detail = NULL
                   WHERE id = %s""",
                (counts["files"], counts["chunks"], repo_id),
            )
            return "restored"

    def delete_repo(self, repo_id: int) -> bool:
        """Remove a project and its files and sections (query logs keep their rows).
        Refused while it is being set up: cancel first, so no half-finished task writes to it."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM repos WHERE id = %s AND NOT (status = ANY(%s)) RETURNING id",
                (repo_id, list(BUSY_STATUSES)),
            ).fetchone()
        return row is not None

    # Elapsed phase time is computed by the database, so a client or app server whose
    # clock differs from a hosted database's still gets a correct time-remaining estimate.
    _REPO_COLUMNS = """*, CASE WHEN status IN ('cloning', 'parsing', 'embedding') AND phase_started_at IS NOT NULL
                       THEN round(extract(epoch FROM now() - phase_started_at)::numeric, 1)::float
                  END AS phase_elapsed_s"""

    def list_repos(self) -> list[dict[str, Any]]:
        """Repos people added. Eval checkouts (eval://…) share the database but stay out of the UI."""
        with self.pool.connection() as conn:
            return conn.execute(
                f"SELECT {self._REPO_COLUMNS} FROM repos WHERE url NOT LIKE 'eval://%%' ORDER BY created_at DESC"
            ).fetchall()

    def get_repo(self, repo_id: int) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            return conn.execute(f"SELECT {self._REPO_COLUMNS} FROM repos WHERE id = %s", (repo_id,)).fetchone()

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
        symbol_terms: Sequence[str] | None = None,
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
                                          content, search_text, symbol_terms, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (repo_id, c.path, c.symbol, c.kind, c.start_line, c.end_line, c.content, st, terms, emb)
                        for c, st, terms, emb in zip(
                            chunks, search_texts, symbol_terms or [""] * len(chunks), embeddings, strict=True
                        )
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
                           ts_rank_cd(tsv, q, 1) * CASE WHEN path ~ %s THEN %s ELSE 1 END AS score
                    FROM chunks, to_tsquery('simple', %s) q
                    WHERE repo_id = %s AND tsv @@ q
                    ORDER BY score DESC LIMIT %s""",
                (TEST_PATH_PATTERN, TEST_PENALTY, tsquery, repo_id, limit),
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
            if is_test_path(hit.path):
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
