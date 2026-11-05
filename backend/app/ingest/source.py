"""Fetch a public GitHub repository and select the files worth indexing."""

import contextlib
import os
import re
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO

from app.models import SourceFile

# Only public github.com repos: keeps the clone step from becoming an SSRF vector.
_GITHUB_URL = re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")

CODE_SUFFIXES = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
INDEXED_SUFFIXES = CODE_SUFFIXES | {".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml"}
# JSON is mostly data or lockfiles; only these few describe the project itself.
INDEXED_NAMES = {"Dockerfile", "Makefile", "package.json", "tsconfig.json", "deno.json"}
# Generated or bundled output: large, unreadable, and it would crowd out the real source.
GENERATED_FILES = re.compile(r"\.(min|bundle|chunk)\.[cm]?js$|\.map$|(^|/)(vendor|vendors)\.[cm]?js$")
SKIPPED_DIRS = {
    ".git",
    ".github",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    # JavaScript build output and caches
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".parcel-cache",
    ".yarn",
    "coverage",
    "bower_components",
}
# Test files, by the usual Python and JavaScript/TypeScript conventions: test/ tests/
# __tests__/ spec/ e2e/ test-d/ directories, test_*.py, *_test.py, *.test.ts, *.spec.js …
# The same pattern runs in Python and in Postgres (its regex syntax is compatible here).
TEST_PATH_PATTERN = (
    r"(^|/)(tests?|__tests__|specs?|e2e|test-d)/"
    r"|(^|/)test_[^/]*\.py$|_test\.py$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
)
_TEST_PATH = re.compile(TEST_PATH_PATTERN)


def is_test_path(rel_path: str) -> bool:
    return _TEST_PATH.search(rel_path) is not None


# Never index anything that commonly holds secrets.
SECRET_PATTERNS = (re.compile(r"(^|/)\.env"), re.compile(r"\.(pem|key|p12|pfx)$"), re.compile(r"id_rsa"))


class InvalidRepoUrl(ValueError):
    pass


def parse_github_url(url: str) -> tuple[str, str]:
    """Return (canonical_url, "owner/name") or raise InvalidRepoUrl."""
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise InvalidRepoUrl("Paste the link to a public GitHub project, like https://github.com/encode/starlette")
    owner, name = match["owner"], match["name"]
    return f"https://github.com/{owner}/{name}", f"{owner}/{name}"


# Git's progress lines, e.g. "Receiving objects:  45% (75/166), 1.20 MiB | 2.00 MiB/s".
_GIT_PROGRESS = re.compile(
    r"^(?:remote:\s*)?(?P<phase>[A-Za-z ]+?):\s+(?P<pct>\d+)%\s*\(\d+/\d+\)"
    r"(?:,\s*(?P<size>[\d.]+\s*[KMG]?i?B))?"
)
# Where each git stage sits on the overall 0-100% clone bar. Downloading dominates.
_CLONE_STAGES = {
    "Counting objects": (0, 3, "Waiting for GitHub"),
    "Compressing objects": (3, 5, "Waiting for GitHub"),
    "Receiving objects": (5, 85, "Downloading"),
    "Resolving deltas": (85, 95, "Unpacking"),
    "Updating files": (95, 100, "Unpacking files"),
}

CloneProgress = Callable[[int, str], None]  # (overall percent, human-readable detail)


def parse_git_progress(line: str) -> tuple[int, str] | None:
    """Map one git progress line to (overall clone percent, detail), or None if irrelevant."""
    match = _GIT_PROGRESS.match(line.strip())
    if not match or match["phase"] not in _CLONE_STAGES:
        return None
    low, high, label = _CLONE_STAGES[match["phase"]]
    overall = low + (high - low) * min(int(match["pct"]), 100) // 100
    detail = f"{label} · {_human_size(match['size'])}" if match["size"] else label
    return overall, detail


_UNITS = {"": 0, "K": 1, "M": 2, "G": 3}


def _human_size(git_size: str) -> str:
    """Git reports binary units ("2.67 MiB"); show familiar decimal ones ("2.8 MB")."""
    match = re.match(r"([\d.]+)\s*([KMG]?)i?B", git_size)
    if not match:
        return git_size
    size = float(match[1]) * 1024 ** _UNITS[match[2]]
    if size >= 1e9:
        return f"{size / 1e9:.1f} GB"
    if size >= 1e6:
        return f"{size / 1e6:.1f} MB"
    return f"{max(size / 1e3, 0.1):.0f} kB" if size >= 1e3 else f"{size:.0f} bytes"


def _lines(stream: IO[bytes]) -> Iterator[str]:
    """Split git's stderr on both \\r (in-place progress updates) and \\n."""
    fd, buffer = stream.fileno(), b""
    while chunk := os.read(fd, 4096):
        buffer += chunk
        *complete, buffer = re.split(rb"[\r\n]", buffer)
        for raw in complete:
            if raw.strip():
                yield raw.decode(errors="replace")
    if buffer.strip():
        yield buffer.decode(errors="replace")


def kill_process_tree(proc: "subprocess.Popen[bytes]", windows: bool = os.name == "nt") -> None:
    """Stop a process and every child it started.

    Killing only the parent would leave children holding its output pipe open, so a reader
    would wait for them. POSIX: kill the process group (the process was started in its own
    session). Windows has no process groups in that sense: `taskkill /T` stops the tree.
    """
    if windows:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30, check=False
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()  # taskkill unavailable: at least stop git itself
            return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def shallow_clone(url: str, dest: Path, timeout_s: int, on_progress: CloneProgress | None = None) -> str:
    """Clone the default branch at depth 1, reporting progress, and return the commit SHA."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    cmd = ["git", "clone", "--progress", "--depth", "1", "--single-branch", "--no-tags", url, str(dest)]
    # Own process group (POSIX; Windows ignores this): git spawns helpers (git-remote-https,
    # index-pack) that inherit the stderr pipe, so the whole tree must be stopped on timeout.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, start_new_session=True)
    assert proc.stderr is not None
    timed_out = threading.Event()

    def kill() -> None:  # a timer, because a stalled git prints nothing we could react to
        timed_out.set()
        kill_process_tree(proc)

    timer = threading.Timer(timeout_s, kill)
    timer.start()
    tail: deque[str] = deque(maxlen=20)  # last lines, for the error message
    try:
        for line in _lines(proc.stderr):
            tail.append(line)
            if on_progress and (progress := parse_git_progress(line)):
                on_progress(*progress)
        proc.wait()
    finally:
        timer.cancel()
        proc.stderr.close()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout_s)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stderr="\n".join(tail))

    sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return sha.stdout.strip()


def is_indexable(rel_path: str) -> bool:
    parts = rel_path.split("/")
    if any(part in SKIPPED_DIRS for part in parts[:-1]):
        return False
    if any(p.search(rel_path) for p in SECRET_PATTERNS) or GENERATED_FILES.search(rel_path):
        return False
    name = parts[-1]
    return name in INDEXED_NAMES or Path(name).suffix.lower() in INDEXED_SUFFIXES


def candidate_paths(root: Path, max_files: int, max_file_bytes: int) -> list[tuple[Path, str]]:
    """Files worth reading, as (absolute path, repo-relative path). Cheap: names and sizes only,
    so the parsing step knows its total up front and can report progress."""
    out: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if is_indexable(rel) and path.stat().st_size <= max_file_bytes:
            out.append((path, rel))
            if len(out) >= max_files:
                break
    return out


def read_source(path: Path, rel: str) -> SourceFile | None:
    """The file as text, or None for binary/undecodable or empty files."""
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    return SourceFile(path=rel, content=content) if content.strip() else None


def iter_source_files(root: Path, max_files: int, max_file_bytes: int) -> Iterator[SourceFile]:
    for path, rel in candidate_paths(root, max_files, max_file_bytes):
        if source := read_source(path, rel):
            yield source
