"""Shared setup for evals: load the question set, get a pinned checkout, index it."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.db import apply_schema, create_pool
from app.embeddings import Embedder, create_embedder
from app.ingest.pipeline import IngestionPipeline
from app.store import RepoStore

EVALS_DIR = Path(__file__).parent
CACHE_DIR = EVALS_DIR / ".cache"


@dataclass
class Question:
    id: str
    category: str
    question: str
    expected_files: list[str] = field(default_factory=list)


@dataclass
class EvalSet:
    repo: str
    commit: str
    questions: list[Question]


def load_eval_set(path: Path = EVALS_DIR / "starlette.yaml") -> EvalSet:
    raw = yaml.safe_load(path.read_text())
    return EvalSet(raw["repo"], raw["commit"], [Question(**q) for q in raw["questions"]])


def pinned_checkout(repo: str, commit: str) -> Path:
    """Fetch exactly `commit` once and reuse it, so results are reproducible."""
    dest = CACHE_DIR / f"{repo.replace('/', '__')}@{commit[:12]}"
    if (dest / ".git").exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", f"https://github.com/{repo}"],
        ["git", "fetch", "-q", "--depth", "1", "origin", commit],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ):
        subprocess.run(cmd, cwd=dest, check=True)
    return dest


@dataclass
class EvalContext:
    settings: Settings
    store: RepoStore
    embedder: Embedder
    repo: dict[str, Any]


def index_eval_repo(eval_set: EvalSet, settings: Settings, reindex: bool = False) -> EvalContext:
    pool = create_pool(settings.database_url)
    apply_schema(pool, settings.embedding_dim)
    store = RepoStore(pool)
    embedder = create_embedder(settings.embedding_backend, settings.embedding_model, settings.embedding_dim)

    # A distinct URL per commit + embedding model, so evals never clobber the user's repos.
    url = f"eval://{eval_set.repo}@{eval_set.commit[:12]}/{settings.embedding_backend}"
    existing = store.find_repo_by_url(url)
    if existing and existing["status"] == "ready" and not reindex:
        return EvalContext(settings, store, embedder, existing)

    checkout = pinned_checkout(eval_set.repo, eval_set.commit)
    repo_id = store.upsert_repo(url, f"{eval_set.repo} (eval)")
    IngestionPipeline(store, embedder, settings).run(repo_id, url, local_path=checkout)
    store.set_status(repo_id, "ready", commit_sha=eval_set.commit)
    repo = store.get_repo(repo_id)
    assert repo is not None and repo["status"] == "ready", repo
    return EvalContext(settings, store, embedder, repo)
