"""HTTP contract tests through FastAPI's TestClient."""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_agent import ScriptedLLM, answer_turn


@pytest.fixture
def client(settings, embedder, store, indexed_repo):
    llm = ScriptedLLM([answer_turn("Prices include tax [shop/pricing.py:L27-L29].")])
    with TestClient(create_app(settings, embedder=embedder, llm=llm)) as c:
        yield c


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_rejects_non_github_urls(client):
    resp = client.post("/api/repos", json={"url": "https://example.com/x/y"})
    assert resp.status_code == 422


def test_lists_repos(client, indexed_repo):
    names = [r["name"] for r in client.get("/api/repos").json()]
    assert indexed_repo["name"] in names


def test_get_file_and_404(client, indexed_repo):
    rid = indexed_repo["id"]
    assert "class Cart" in client.get(f"/api/repos/{rid}/file", params={"path": "shop/pricing.py"}).json()["content"]
    assert client.get(f"/api/repos/{rid}/file", params={"path": "nope.py"}).status_code == 404


def test_ask_streams_server_sent_events(client, indexed_repo):
    resp = client.post(f"/api/repos/{indexed_repo['id']}/ask", json={"question": "Is tax included?", "mode": "quick"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    assert events[0]["type"] == "step" and events[-1]["type"] == "done"


def test_ask_validates_input(client, indexed_repo):
    rid = indexed_repo["id"]
    assert client.post(f"/api/repos/{rid}/ask", json={"question": "x" * 5000}).status_code == 422
    assert client.post("/api/repos/99999/ask", json={"question": "hello?"}).status_code == 404


def test_ask_without_api_key_is_a_clear_503(settings, embedder, store, indexed_repo):
    app = create_app(settings.model_copy(update={"anthropic_api_key": None}), embedder=embedder, llm=ScriptedLLM([]))
    with TestClient(app) as c:
        resp = c.post(f"/api/repos/{indexed_repo['id']}/ask", json={"question": "hello?"})
        config = c.get("/api/config").json()
    assert config["llm_configured"] is False and config["provider"] == "anthropic"
    assert resp.status_code == 503 and "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_startup_marks_interrupted_indexing_as_failed(settings, embedder, store):
    repo_id = store.upsert_repo("https://github.com/example/stuck", "example/stuck")
    store.set_status(repo_id, "embedding")
    with TestClient(create_app(settings, embedder=embedder, llm=ScriptedLLM([]))):
        repo = store.get_repo(repo_id)
    assert repo is not None and repo["status"] == "failed" and "interrupted" in repo["error"]
