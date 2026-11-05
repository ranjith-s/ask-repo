"""clone -> select files -> chunk -> embed -> store, with status updates."""

import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from app.config import Settings
from app.embeddings import Embedder
from app.ingest.chunker import chunk_file, embedding_text, search_text, symbol_terms
from app.ingest.source import CloneCancelled, candidate_paths, read_source, shallow_clone
from app.models import Chunk, SourceFile
from app.observability import log
from app.store import RepoStore


class IndexingCancelled(Exception):
    """Someone cancelled setting up this project."""


class ProgressReporter:
    """Writes phase progress to the repo row, at most every `min_interval_s` seconds.

    Git and the parser can report hundreds of updates a second; the UI polls every 1.5 s,
    so throttling keeps database writes (possibly to a hosted DB) to a couple per second.
    """

    def __init__(self, store: RepoStore, repo_id: int, min_interval_s: float = 0.5) -> None:
        self.store = store
        self.repo_id = repo_id
        self.min_interval_s = min_interval_s
        self._last_write = float("-inf")
        self._last_value: tuple[int, int | None, str | None] | None = None
        self._last_check = float("-inf")
        self._cancelled = False

    def cancel_requested(self) -> bool:
        """Whether someone asked to stop; reads the DB at most every `min_interval_s`."""
        now = time.monotonic()
        if not self._cancelled and now - self._last_check >= self.min_interval_s:
            self._cancelled = self.store.is_cancel_requested(self.repo_id)
            self._last_check = now
        return self._cancelled

    def check_cancelled(self) -> None:
        if self.cancel_requested():
            raise IndexingCancelled()

    def start_phase(self, status: str, total: int | None, **fields: Any) -> None:
        """Enter a new phase: resets progress in the DB and forgets what was last written."""
        self.store.start_phase(self.repo_id, status, total, **fields)
        self._last_write, self._last_value = float("-inf"), None

    def report(self, done: int, total: int | None, detail: str | None = None, force: bool = False) -> None:
        value = (done, total, detail)
        now = time.monotonic()
        if value == self._last_value or (not force and now - self._last_write < self.min_interval_s):
            return
        self.store.set_progress(self.repo_id, done, total, detail)
        self._last_write, self._last_value = now, value


class IngestionPipeline:
    def __init__(
        self,
        store: RepoStore,
        embedder: Embedder,
        settings: Settings,
        embed_batch_size: int = 128,
        progress_interval_s: float = 0.5,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.settings = settings
        self.embed_batch_size = embed_batch_size  # a batch takes ~1-3 s on CPU
        self.progress_interval_s = progress_interval_s

    def run(self, repo_id: int, url: str, local_path: Path | None = None) -> None:
        """Index a repo. `local_path` skips the clone (used by tests and evals)."""
        started = time.perf_counter()
        bound = log.bind(repo_id=repo_id, url=url)
        progress = ProgressReporter(self.store, repo_id, self.progress_interval_s)
        try:
            progress.check_cancelled()  # cancelled while it was still waiting to start
            with tempfile.TemporaryDirectory(prefix="askrepo-") as tmp:
                root = local_path or Path(tmp) / "repo"
                commit = None
                if local_path is None:
                    progress.start_phase("cloning", total=100)  # git reports a percentage
                    commit = shallow_clone(
                        url,
                        root,
                        self.settings.clone_timeout_s,
                        on_progress=lambda pct, detail: progress.report(pct, 100, detail),
                        should_stop=progress.cancel_requested,
                    )
                    progress.report(100, 100, "Downloaded", force=True)

                files, chunks = self._parse(root, progress)
                if not chunks:
                    raise ValueError(
                        "This project has no Python, JavaScript or TypeScript code or documentation to read."
                    )

                progress.start_phase("embedding", total=len(chunks), file_count=len(files), chunk_count=len(chunks))
                embeddings = self._embed([embedding_text(c) for c in chunks], progress)

                progress.check_cancelled()  # last chance: after this the new index replaces the old
                progress.report(len(chunks), len(chunks), "Saving", force=True)
                self.store.replace_contents(
                    repo_id,
                    files,
                    chunks,
                    [search_text(c) for c in chunks],
                    embeddings,
                    [symbol_terms(c) for c in chunks],
                )
                self.store.set_status(repo_id, "ready", commit_sha=commit)
                bound.info(
                    "ingest.done",
                    files=len(files),
                    chunks=len(chunks),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
        except (IndexingCancelled, CloneCancelled):
            outcome = self.store.finish_cancel(repo_id)
            bound.info("ingest.cancelled", outcome=outcome, duration_ms=int((time.perf_counter() - started) * 1000))
        except Exception as exc:  # report any failure on the repo row, never crash the worker
            bound.exception("ingest.failed")
            self.store.set_status(repo_id, "failed", error=_friendly_error(exc))

    def _parse(self, root: Path, progress: ProgressReporter) -> tuple[list[SourceFile], list[Chunk]]:
        paths = candidate_paths(root, self.settings.max_files, self.settings.max_file_bytes)
        progress.start_phase("parsing", total=len(paths))
        files: list[SourceFile] = []
        chunks: list[Chunk] = []
        for done, (path, rel) in enumerate(paths, start=1):
            progress.check_cancelled()
            if source := read_source(path, rel):
                files.append(source)
                chunks.extend(chunk_file(source))
            progress.report(done, len(paths), rel)
        progress.report(len(paths), len(paths), None, force=True)
        return files, chunks

    def _embed(self, texts: list[str], progress: ProgressReporter) -> list[np.ndarray]:
        embeddings: list[np.ndarray] = []
        for start in range(0, len(texts), self.embed_batch_size):
            progress.check_cancelled()
            embeddings.extend(self.embedder.embed_documents(texts[start : start + self.embed_batch_size]))
            # Force every batch: batches are already seconds apart, and each one moves the bar.
            progress.report(len(embeddings), len(texts), force=True)
        return embeddings


def _friendly_error(exc: Exception) -> str:
    import subprocess

    if isinstance(exc, subprocess.TimeoutExpired):
        return "Downloading took too long. The project may be too large."
    if isinstance(exc, subprocess.CalledProcessError):
        return "Couldn't download this project. Check the link and that the project is public."
    return str(exc)[:300]
