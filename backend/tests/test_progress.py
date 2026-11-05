"""Indexing progress for cloning, parsing and embedding."""

import subprocess
import time
from pathlib import Path

import pytest

from app.ingest.pipeline import IngestionPipeline, ProgressReporter
from app.ingest.source import parse_git_progress, shallow_clone
from tests.conftest import FIXTURE_REPO

# Exact lines from a real `git clone --progress` of encode/starlette (trailing spaces included).
REAL_GIT_LINES = [
    ("Cloning into 'starlette'...", None),
    ("remote: Enumerating objects: 166, done.        ", None),
    ("remote: Counting objects:   0% (1/166)        ", (0, "Waiting for GitHub")),
    ("remote: Compressing objects: 100% (156/156), done.        ", (5, "Waiting for GitHub")),
    ("Receiving objects:   0% (1/166)", (5, "Downloading")),
    ("remote: Total 166 (delta 6), reused 63 (delta 1), pack-reused 0 (from 0)        ", None),
    ("Receiving objects:  99% (165/166)", (84, "Downloading")),
    ("Receiving objects: 100% (166/166), 2.67 MiB | 16.75 MiB/s, done.", (85, "Downloading · 2.8 MB")),
    ("Resolving deltas:   0% (0/6)", (85, "Unpacking")),
    ("Resolving deltas: 100% (6/6), done.", (95, "Unpacking")),
    ("Updating files:  50% (60/120)", (97, "Unpacking files")),
]


@pytest.mark.parametrize("line,expected", REAL_GIT_LINES)
def test_parse_git_progress_on_real_output(line, expected):
    assert parse_git_progress(line) == expected


# ---- shallow_clone with a fake git -------------------------------------------------

FAKE_GIT = r"""#!/bin/sh
if [ "$1" = "-C" ]; then echo 0123456789abcdef; exit 0; fi
for arg; do dest="$arg"; done
printf "Cloning into 'x'...\n" >&2
printf 'remote: Counting objects:  50%% (1/2)   \rremote: Counting objects: 100%% (2/2), done.\n' >&2
printf 'Receiving objects:  40%% (2/5)\rReceiving objects: 100%% (5/5), 2.50 MiB | 2.00 MiB/s, done.\n' >&2
if [ -n "$FAKE_GIT_SLEEP" ]; then sleep "$FAKE_GIT_SLEEP"; fi
if [ -n "$FAKE_GIT_FAIL" ]; then printf 'fatal: repository not found\n' >&2; exit 128; fi
printf 'Resolving deltas: 100%% (1/1), done.' >&2
cp -r "$FAKE_GIT_SOURCE" "$dest"
"""


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text(FAKE_GIT)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("FAKE_GIT_SOURCE", str(FIXTURE_REPO))
    return monkeypatch


def test_clone_streams_progress_including_in_place_updates(fake_git, tmp_path):
    seen: list[tuple[int, str]] = []
    sha = shallow_clone("https://github.com/x/y", tmp_path / "out", 10, on_progress=lambda p, d: seen.append((p, d)))
    assert sha == "0123456789abcdef"
    assert seen == [
        (1, "Waiting for GitHub"),  # \r-separated update, not lost
        (3, "Waiting for GitHub"),
        (37, "Downloading"),
        (85, "Downloading · 2.6 MB"),
        (95, "Unpacking"),  # last line has no trailing newline
    ]
    assert (tmp_path / "out" / "shop" / "pricing.py").exists()


def test_clone_failure_keeps_gits_message(fake_git, tmp_path):
    fake_git.setenv("FAKE_GIT_FAIL", "1")
    with pytest.raises(subprocess.CalledProcessError) as info:
        shallow_clone("https://github.com/x/missing", tmp_path / "out", 10)
    assert "repository not found" in info.value.stderr


def test_stalled_clone_is_killed_at_the_timeout(fake_git, tmp_path):
    fake_git.setenv("FAKE_GIT_SLEEP", "30")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        shallow_clone("https://github.com/x/slow", tmp_path / "out", 1)
    assert time.monotonic() - started < 10


# ---- ProgressReporter throttling ---------------------------------------------------


class RecordingStore:
    def __init__(self) -> None:
        self.writes: list[tuple] = []

    def set_progress(self, repo_id, done, total, detail=None):
        self.writes.append(("progress", done, total, detail))

    def start_phase(self, repo_id, status, total, **fields):
        self.writes.append(("phase", status, total))


def test_reporter_throttles_dedupes_and_resets_per_phase():
    store = RecordingStore()
    reporter = ProgressReporter(store, 1, min_interval_s=60)  # nothing passes the timer in this test
    for i in range(1, 500):
        reporter.report(i, 500, "file.py")
    assert store.writes == [("progress", 1, 500, "file.py")]  # first write is immediate, rest throttled

    reporter.report(500, 500, None, force=True)
    reporter.report(500, 500, None, force=True)  # identical: skipped even when forced
    assert store.writes[-1] == ("progress", 500, 500, None) and len(store.writes) == 2

    reporter.start_phase("embedding", 10)
    reporter.report(500, 500, None, force=True)  # same value as before, but a new phase: written
    assert store.writes[-2:] == [("phase", "embedding", 10), ("progress", 500, 500, None)]


# ---- The pipeline end to end ---------------------------------------------------------


def run_and_record(store, embedder, settings, url: str, local_path: Path | None):
    """Run the pipeline and snapshot the repo row after every progress write."""
    repo_id = store.upsert_repo(url, url.rsplit("/", 1)[-1])
    snapshots: list[tuple] = []
    write = store.set_progress

    def spy(rid, done, total, detail=None):
        write(rid, done, total, detail)
        row = store.get_repo(rid)
        snapshots.append(
            (row["status"], row["progress_done"], row["progress_total"], row["progress_detail"], row["phase_elapsed_s"])
        )

    store.set_progress = spy  # type: ignore[method-assign]
    try:
        IngestionPipeline(store, embedder, settings, embed_batch_size=4, progress_interval_s=0).run(
            repo_id, url, local_path
        )
    finally:
        del store.set_progress
    return store.get_repo(repo_id), snapshots


def phase(snapshots, status):
    return [s for s in snapshots if s[0] == status]


def test_every_phase_reports_progress_from_start_to_finish(store, embedder, settings, fake_git):
    repo, snaps = run_and_record(store, embedder, settings, "https://github.com/example/all-phases", None)
    assert repo["status"] == "ready" and repo["commit_sha"] == "0123456789abcdef"

    cloning, parsing, embedding = phase(snaps, "cloning"), phase(snaps, "parsing"), phase(snaps, "embedding")
    assert [s[1] for s in cloning][-1] == 100 and cloning[-1][3] == "Downloaded"
    assert {s[2] for s in cloning} == {100}
    assert any(s[3] == "Downloading · 2.6 MB" for s in cloning)

    files = [s for s in parsing if s[3]]  # per-file updates carry the path being parsed
    assert parsing[-1][1] == parsing[-1][2] == 4  # README + 3 Python files; .env is never indexed
    assert {s[3] for s in files} >= {"shop/pricing.py", "README.md"}

    counts = [s[1] for s in embedding]
    assert counts == sorted(counts) and counts[-1] == repo["chunk_count"] == embedding[0][2]
    assert embedding[-1][3] == "Saving"

    for status in ("cloning", "parsing", "embedding"):  # elapsed time only while busy
        assert all(s[4] is not None and s[4] >= 0 for s in phase(snaps, status))
    assert repo["phase_elapsed_s"] is None


def test_reindexing_starts_from_zero(store, embedder, settings):
    url = "https://github.com/example/reindex"
    run_and_record(store, embedder, settings, url, FIXTURE_REPO)
    store.upsert_repo(url, "reindex")
    pending = store.find_repo_by_url(url)
    assert pending["status"] == "pending" and pending["progress_done"] == 0 and pending["progress_total"] is None
