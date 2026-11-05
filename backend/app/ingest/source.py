"""Fetch a public GitHub repository and select the files worth indexing."""

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from app.models import SourceFile

# Only public github.com repos: keeps the clone step from becoming an SSRF vector.
_GITHUB_URL = re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$")

INDEXED_SUFFIXES = {".py", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml"}
INDEXED_NAMES = {"Dockerfile", "Makefile"}
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
}
# Never index anything that commonly holds secrets.
SECRET_PATTERNS = (re.compile(r"(^|/)\.env"), re.compile(r"\.(pem|key|p12|pfx)$"), re.compile(r"id_rsa"))


class InvalidRepoUrl(ValueError):
    pass


def parse_github_url(url: str) -> tuple[str, str]:
    """Return (canonical_url, "owner/name") or raise InvalidRepoUrl."""
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise InvalidRepoUrl("Only public https://github.com/<owner>/<repo> URLs are supported")
    owner, name = match["owner"], match["name"]
    return f"https://github.com/{owner}/{name}", f"{owner}/{name}"


def shallow_clone(url: str, dest: Path, timeout_s: int) -> str:
    """Clone the default branch at depth 1 and return the commit SHA."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    subprocess.run(
        ["git", "clone", "--depth", "1", "--single-branch", "--no-tags", url, str(dest)],
        check=True,
        capture_output=True,
        timeout=timeout_s,
        env=env,
    )
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
    if any(p.search(rel_path) for p in SECRET_PATTERNS):
        return False
    name = parts[-1]
    return name in INDEXED_NAMES or Path(name).suffix.lower() in INDEXED_SUFFIXES


def iter_source_files(root: Path, max_files: int, max_file_bytes: int) -> Iterator[SourceFile]:
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if not is_indexable(rel) or path.stat().st_size > max_file_bytes:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not content.strip():
            continue
        yield SourceFile(path=rel, content=content)
        count += 1
        if count >= max_files:
            return
