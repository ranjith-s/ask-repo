"""clone -> select files -> chunk -> embed -> store, with status updates."""

import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.embeddings import Embedder
from app.ingest.chunker import chunk_file, embedding_text, search_text
from app.ingest.source import iter_source_files, shallow_clone
from app.observability import log
from app.store import RepoStore


class IngestionPipeline:
    def __init__(self, store: RepoStore, embedder: Embedder, settings: Settings) -> None:
        self.store = store
        self.embedder = embedder
        self.settings = settings

    def run(self, repo_id: int, url: str, local_path: Path | None = None) -> None:
        """Index a repo. `local_path` skips the clone (used by tests and evals)."""
        started = time.perf_counter()
        bound = log.bind(repo_id=repo_id, url=url)
        try:
            with tempfile.TemporaryDirectory(prefix="askrepo-") as tmp:
                root = local_path or Path(tmp) / "repo"
                commit = None
                if local_path is None:
                    self.store.set_status(repo_id, "cloning")
                    commit = shallow_clone(url, root, self.settings.clone_timeout_s)

                self.store.set_status(repo_id, "parsing")
                files = list(iter_source_files(root, self.settings.max_files, self.settings.max_file_bytes))
                chunks = [chunk for f in files for chunk in chunk_file(f)]
                if not chunks:
                    raise ValueError("No indexable files found (Python, Markdown or config)")

                self.store.set_status(repo_id, "embedding", file_count=len(files), chunk_count=len(chunks))
                embeddings = self.embedder.embed_documents([embedding_text(c) for c in chunks])

                self.store.replace_contents(repo_id, files, chunks, [search_text(c) for c in chunks], embeddings)
                self.store.set_status(repo_id, "ready", commit_sha=commit)
                bound.info(
                    "ingest.done",
                    files=len(files),
                    chunks=len(chunks),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
        except Exception as exc:  # report any failure on the repo row, never crash the worker
            bound.exception("ingest.failed")
            self.store.set_status(repo_id, "failed", error=_friendly_error(exc))


def _friendly_error(exc: Exception) -> str:
    import subprocess

    if isinstance(exc, subprocess.TimeoutExpired):
        return "Clone timed out: the repository may be too large."
    if isinstance(exc, subprocess.CalledProcessError):
        return "Could not clone the repository. Is it public and does it exist?"
    return str(exc)[:300]
