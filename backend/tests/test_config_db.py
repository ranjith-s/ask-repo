"""Local (non-Docker) setup: shared .env file and a clear error without pgvector."""

import os

import psycopg
import pytest

from app.config import ENV_FILES, Settings  # conftest disables env files for other tests
from app.db import PGVECTOR_MISSING, create_pool


def test_root_env_is_read_and_backend_env_overrides_it(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("LLM_PROVIDER=gemini\nLLM_MODEL=from-root\nGEMINI_API_KEY=root-key\n")
    backend = tmp_path / "backend"
    backend.mkdir()
    monkeypatch.chdir(backend)
    settings = Settings(_env_file=ENV_FILES)
    assert (settings.llm_provider, settings.model) == ("gemini", "from-root")

    (backend / ".env").write_text("LLM_MODEL=from-backend\n")
    settings = Settings(_env_file=ENV_FILES)
    assert settings.model == "from-backend" and settings.gemini_api_key == "root-key"

    monkeypatch.setenv("LLM_MODEL", "from-env")
    assert Settings(_env_file=ENV_FILES).model == "from-env"  # real environment variables win


@pytest.mark.skipif("PLAIN_POSTGRES_URL" not in os.environ, reason="needs a Postgres without pgvector")
def test_missing_pgvector_gives_an_actionable_error():
    with pytest.raises(RuntimeError) as info:
        create_pool(os.environ["PLAIN_POSTGRES_URL"])
    assert str(info.value) == PGVECTOR_MISSING and "pgvector" in str(info.value)


def test_pool_disables_prepared_statements_for_transaction_poolers(store):
    # PgBouncer / Supabase port 6543 hand each transaction to a different server
    # connection, so server-side prepared statements break after a few queries.
    assert store.pool.kwargs["prepare_threshold"] is None


def test_pool_recovers_when_the_server_drops_its_connections(store):
    """Hosted databases close idle connections (Neon suspends); the next query must still work."""
    store.list_repos()  # make sure the pool holds open connections
    url = store.pool.conninfo
    with psycopg.connect(url, autocommit=True) as admin:
        killed = admin.execute(
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchone()
    assert killed is not None and killed[0] >= 1
    for _ in range(3):  # one attempt per possibly-dead pooled connection
        assert isinstance(store.list_repos(), list)
