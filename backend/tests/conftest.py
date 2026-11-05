import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app.config import Settings
from app.db import apply_schema, create_pool
from app.embeddings import HashEmbedder
from app.ingest.pipeline import IngestionPipeline
from app.store import RepoStore

# Tests must not depend on the developer's own .env or exported LLM settings.
Settings.model_config["env_file"] = None
for _name in list(os.environ):
    if _name.startswith(("LLM_", "ANTHROPIC_", "GEMINI_", "OLLAMA_", "PRICE_", "EMBEDDING_")):
        del os.environ[_name]

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://askrepo:askrepo@localhost:5432/askrepo_test")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=TEST_DB_URL,
        embedding_backend="hash",
        anthropic_api_key="test-key",
        log_json=False,
        static_dir="/nonexistent",
    )


@pytest.fixture(scope="session")
def store(settings: Settings) -> Iterator[RepoStore]:
    """A real Postgres+pgvector database; tests that need it are skipped if it isn't running."""
    admin_url = TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"
    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
            conn.execute("DROP DATABASE IF EXISTS askrepo_test WITH (FORCE)")
            conn.execute("CREATE DATABASE askrepo_test")
    except psycopg.OperationalError:
        pytest.skip("Postgres not available (start it with `make db`)")
    pool = create_pool(TEST_DB_URL)
    apply_schema(pool, settings.embedding_dim)
    yield RepoStore(pool)
    pool.close()


@pytest.fixture(scope="session")
def embedder(settings: Settings) -> HashEmbedder:
    return HashEmbedder(settings.embedding_dim)


@pytest.fixture(scope="session")
def indexed_repo(store: RepoStore, embedder: HashEmbedder, settings: Settings) -> dict:
    repo_id = store.upsert_repo("https://github.com/example/sample", "example/sample")
    IngestionPipeline(store, embedder, settings).run(repo_id, "local", local_path=FIXTURE_REPO)
    repo = store.get_repo(repo_id)
    assert repo is not None and repo["status"] == "ready", repo
    return repo
