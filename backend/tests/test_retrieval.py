"""Ingestion + retrieval against a real Postgres, using the offline hash embedder."""

import pytest

from app.agent.tools import RepoTools

pytestmark = pytest.mark.usefixtures("indexed_repo")


def test_ingestion_records_counts_and_skips_secrets(store, indexed_repo):
    paths = store.list_paths(indexed_repo["id"])
    assert "shop/pricing.py" in paths and "README.md" in paths
    assert ".env" not in paths
    assert indexed_repo["file_count"] == len(paths)
    assert indexed_repo["chunk_count"] > len(paths)


def test_keyword_search_finds_identifier(store, indexed_repo):
    hits = store.keyword_search(indexed_repo["id"], "where is total_with_tax computed", limit=5)
    assert hits[0].symbol == "Cart.total_with_tax"


def test_hybrid_search_returns_fused_results(store, embedder, indexed_repo):
    question = "how is a discount applied to the price"
    hits = store.hybrid_search(indexed_repo["id"], question, embedder.embed_query(question), limit=5)
    assert "apply_discount" in [h.symbol for h in hits[:3]]
    assert hits == sorted(hits, key=lambda h: h.score, reverse=True)


@pytest.fixture
def tools(store, embedder, indexed_repo):
    return RepoTools(store, embedder, indexed_repo["id"], max_chars=2000)


async def test_read_file_returns_numbered_lines_and_marks_them_seen(tools):
    out = await tools.run("read_file", {"path": "shop/pricing.py", "start_line": 8, "end_line": 9})
    assert not out.is_error
    assert out.content.splitlines()[0].strip().startswith("8  def apply_discount")
    assert tools.seen.overlaps("shop/pricing.py", 9, 9)


async def test_read_file_rejects_unknown_paths_without_touching_disk(tools):
    out = await tools.run("read_file", {"path": "../../etc/passwd", "start_line": 1, "end_line": 5})
    assert out.is_error and "not found" in out.content.lower()


async def test_grep_reports_path_and_line(tools):
    out = await tools.run("grep", {"pattern": "apply_discount(", "path_prefix": ""})
    assert "shop/api.py:L7:" in out.content and "shop/pricing.py:L8:" in out.content


async def test_grep_like_wildcards_are_escaped(tools):
    out = await tools.run("grep", {"pattern": "Cart", "path_prefix": "sh%"})
    assert out.content == "No matches."


async def test_invalid_tool_args_become_errors_not_exceptions(tools):
    assert (await tools.run("grep", {"pattern": "x", "path_prefix": ""})).is_error
    assert (await tools.run("read_file", {"path": "shop/api.py"})).is_error
    assert (await tools.run("rm_rf", {})).is_error


async def test_tool_output_is_truncated(store, embedder, indexed_repo):
    small = RepoTools(store, embedder, indexed_repo["id"], max_chars=50)
    out = await small.run("read_file", {"path": "shop/pricing.py", "start_line": 1, "end_line": 30})
    assert out.content.endswith("[truncated; narrow the request]")


def test_embedding_progress_is_reported_per_batch(store, embedder, settings):
    from app.ingest.pipeline import IngestionPipeline
    from tests.conftest import FIXTURE_REPO

    repo_id = store.upsert_repo("https://github.com/example/progress", "example/progress")
    seen: list[tuple[str, int, int, float | None]] = []
    record = store.set_embedding_progress

    def spy(rid: int, embedded: int) -> None:
        record(rid, embedded)
        row = store.get_repo(rid)
        assert row is not None
        seen.append((row["status"], row["embedded_count"], row["chunk_count"], row["embedding_elapsed_s"]))

    store.set_embedding_progress = spy  # type: ignore[method-assign]
    try:
        IngestionPipeline(store, embedder, settings, embed_batch_size=4).run(repo_id, "local", FIXTURE_REPO)
    finally:
        del store.set_embedding_progress  # back to the class method

    total = seen[0][2]
    counts = [count for _, count, _, _ in seen]
    assert counts == sorted(counts) and counts[-1] == total and len(counts) == -(-total // 4)
    assert all(status == "embedding" and elapsed is not None and elapsed >= 0 for status, _, _, elapsed in seen)

    done = store.get_repo(repo_id)
    assert done is not None and done["status"] == "ready"
    assert done["embedded_count"] == done["chunk_count"] and done["embedding_elapsed_s"] is None
