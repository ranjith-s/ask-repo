"""Cancelling a project's setup, and removing a project."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.ingest.pipeline import IngestionPipeline
from app.ingest.source import CloneCancelled, shallow_clone
from app.main import create_app
from tests.conftest import FIXTURE_REPO
from tests.test_agent import ScriptedLLM
from tests.test_progress import fake_git  # noqa: F401  (pytest fixture)


def counts(store, repo_id: int) -> tuple[int, int]:
    with store.pool.connection() as conn:
        row = conn.execute(
            """SELECT (SELECT count(*) FROM files WHERE repo_id = %(id)s) AS files,
                      (SELECT count(*) FROM chunks WHERE repo_id = %(id)s) AS chunks""",
            {"id": repo_id},
        ).fetchone()
    return row["files"], row["chunks"]


class CancelAfterFirstBatch:
    """An embedder that cancels the setup once it has embedded one batch."""

    def __init__(self, inner, store, repo_id: int) -> None:
        self.inner, self.store, self.repo_id, self.dim = inner, store, repo_id, inner.dim
        self.batches = 0

    def embed_documents(self, texts):
        self.batches += 1
        if self.batches == 1:
            self.store.request_cancel(self.repo_id)
        return self.inner.embed_documents(texts)

    def embed_query(self, text):
        return self.inner.embed_query(text)


def pipeline(store, embedder, settings):
    return IngestionPipeline(store, embedder, settings, embed_batch_size=4, progress_interval_s=0)


# ---- the clone ------------------------------------------------------------------------


def test_cancel_stops_a_silent_clone_quickly(fake_git, tmp_path):  # noqa: F811
    fake_git.setenv("FAKE_GIT_SLEEP", "30")  # git goes quiet: no output to react to
    started = time.monotonic()
    with pytest.raises(CloneCancelled):
        shallow_clone(
            "https://github.com/x/y", tmp_path / "out", 60, should_stop=lambda: time.monotonic() - started > 0.5
        )
    assert time.monotonic() - started < 5


def test_cancelling_during_the_download_removes_a_new_project(store, embedder, settings, fake_git):  # noqa: F811
    fake_git.setenv("FAKE_GIT_SLEEP", "30")
    repo_id = store.upsert_repo("https://github.com/example/cancel-clone", "example/cancel-clone")
    threading.Timer(0.5, store.request_cancel, args=(repo_id,)).start()
    started = time.monotonic()
    pipeline(store, embedder, settings).run(repo_id, "https://github.com/example/cancel-clone")
    assert time.monotonic() - started < 5
    assert store.get_repo(repo_id) is None


# ---- parsing and embedding --------------------------------------------------------------


def test_cancelled_before_it_starts(store, embedder, settings):
    repo_id = store.upsert_repo("https://github.com/example/cancel-early", "example/cancel-early")
    assert store.request_cancel(repo_id)
    pipeline(store, embedder, settings).run(repo_id, "local", local_path=FIXTURE_REPO)
    assert store.get_repo(repo_id) is None


def test_cancelling_while_embedding_a_new_project_leaves_nothing_behind(store, embedder, settings):
    repo_id = store.upsert_repo("https://github.com/example/cancel-new", "example/cancel-new")
    slow = CancelAfterFirstBatch(embedder, store, repo_id)
    pipeline(store, slow, settings).run(repo_id, "local", local_path=FIXTURE_REPO)
    assert slow.batches == 1  # stopped at the next check, not after embedding everything (3 batches)
    assert store.get_repo(repo_id) is None and counts(store, repo_id) == (0, 0)


def test_cancelling_a_readd_keeps_the_previous_index(store, embedder, settings):
    url = "https://github.com/example/cancel-readd"
    repo_id = store.upsert_repo(url, "example/cancel-readd")
    pipeline(store, embedder, settings).run(repo_id, "local", local_path=FIXTURE_REPO)
    before = store.get_repo(repo_id)
    assert before["status"] == "ready"

    store.upsert_repo(url, "example/cancel-readd")  # add it again, then cancel midway
    pipeline(store, CancelAfterFirstBatch(embedder, store, repo_id), settings).run(
        repo_id, "local", local_path=FIXTURE_REPO
    )
    after = store.get_repo(repo_id)
    assert after["status"] == "ready" and not after["cancel_requested"]
    assert (after["file_count"], after["chunk_count"]) == (before["file_count"], before["chunk_count"])
    assert counts(store, repo_id) == (before["file_count"], before["chunk_count"])
    assert store.keyword_search(repo_id, "total_with_tax", 1)  # still searchable


def test_adding_again_clears_an_old_cancel_request(store):
    url = "https://github.com/example/cancel-reset"
    repo_id = store.upsert_repo(url, "example/cancel-reset")
    store.request_cancel(repo_id)
    store.upsert_repo(url, "example/cancel-reset")
    assert store.get_repo(repo_id)["cancel_requested"] is False


# ---- the API ---------------------------------------------------------------------------


@pytest.fixture
def client(settings, embedder, store):
    with TestClient(create_app(settings, embedder=embedder, llm=ScriptedLLM([]))) as c:
        yield c


def test_cancel_api(client, store):
    repo_id = store.upsert_repo("https://github.com/example/api-cancel", "example/api-cancel")
    store.set_status(repo_id, "embedding")
    resp = client.post(f"/api/repos/{repo_id}/cancel")
    assert resp.status_code == 202 and resp.json()["cancel_requested"] is True
    assert store.is_cancel_requested(repo_id)

    store.set_status(repo_id, "ready")
    assert client.post(f"/api/repos/{repo_id}/cancel").status_code == 409  # nothing to cancel
    assert client.post("/api/repos/999999/cancel").status_code == 404


def test_remove_api_deletes_files_and_sections_but_keeps_question_logs(client, store, embedder, settings):
    repo_id = store.upsert_repo("https://github.com/example/api-remove", "example/api-remove")
    pipeline(store, embedder, settings).run(repo_id, "local", local_path=FIXTURE_REPO)
    store.log_query(repo_id=repo_id, question="kept after removal?", mode="quick")
    assert counts(store, repo_id)[0] > 0

    assert client.delete(f"/api/repos/{repo_id}").status_code == 204
    assert store.get_repo(repo_id) is None and counts(store, repo_id) == (0, 0)
    assert client.delete(f"/api/repos/{repo_id}").status_code == 404
    with store.pool.connection() as conn:
        log = conn.execute("SELECT repo_id FROM query_logs WHERE question = 'kept after removal?'").fetchone()
    assert log is not None and log["repo_id"] is None


def test_remove_is_refused_while_setting_up(client, store):
    repo_id = store.upsert_repo("https://github.com/example/api-busy", "example/api-busy")
    store.set_status(repo_id, "parsing")
    resp = client.delete(f"/api/repos/{repo_id}")
    assert resp.status_code == 409 and "Cancel it first" in resp.json()["detail"]
    assert store.get_repo(repo_id) is not None
