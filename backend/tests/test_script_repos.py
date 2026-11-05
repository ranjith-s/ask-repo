"""A JavaScript/TypeScript project end to end: ingestion, search and the agent's tools."""

from pathlib import Path

import pytest

from app.agent.tools import RepoTools
from app.ingest.pipeline import IngestionPipeline

TS_REPO = Path(__file__).parent / "fixtures" / "sample_ts_repo"


@pytest.fixture(scope="module")
def ts_repo(store, embedder, settings):
    repo_id = store.upsert_repo("https://github.com/example/sample-ts", "example/sample-ts")
    IngestionPipeline(store, embedder, settings).run(repo_id, "local", local_path=TS_REPO)
    repo = store.get_repo(repo_id)
    assert repo is not None and repo["status"] == "ready", repo
    return repo


def test_reads_js_ts_and_package_json_but_not_build_output(store, ts_repo):
    paths = store.list_paths(ts_repo["id"])
    assert paths == ["package.json", "src/cart.ts", "src/routes/checkout.js"]


def test_keyword_search_finds_typescript_methods_and_routes(store, ts_repo):
    hits = store.keyword_search(ts_repo["id"], "where is totalWithTax calculated", limit=3)
    assert hits[0].symbol == "Cart.totalWithTax" and hits[0].path == "src/cart.ts"
    route = store.keyword_search(ts_repo["id"], "checkout endpoint coupon", limit=3)
    # The file's outline (which lists the route) may rank first; the handler itself must be next,
    # above leftover top-level code such as `module.exports = router`.
    assert "router.post /checkout" in [h.symbol for h in route[:2]]
    assert all(h.path == "src/routes/checkout.js" for h in route[:2])


def test_dependencies_question_reaches_package_json(store, ts_repo):
    hits = store.keyword_search(ts_repo["id"], "what dependencies does it use zod express", limit=5)
    assert "package.json" in [h.path for h in hits]


async def test_agent_tools_work_on_typescript(store, embedder, ts_repo):
    tools = RepoTools(store, embedder, ts_repo["id"], max_chars=4000)
    grep = await tools.run("grep", {"pattern": "applyDiscount(", "path_prefix": ""})
    assert "src/cart.ts:L23:" in grep.content and "src/routes/checkout.js:L10:" in grep.content
    read = await tools.run("read_file", {"path": "src/cart.ts", "start_line": 16, "end_line": 20})
    assert "totalWithTax(): number" in read.content and tools.seen.overlaps("src/cart.ts", 17, 17)
