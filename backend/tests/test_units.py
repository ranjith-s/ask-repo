"""Pure-function tests: no database or network."""

import pytest

from app.agent.citations import check_citations
from app.agent.tools import SeenLines, repo_map
from app.ingest.source import InvalidRepoUrl, is_indexable, parse_github_url
from app.models import SearchHit
from app.store import keyword_terms, rrf_fuse


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/encode/starlette", "encode/starlette"),
        ("https://github.com/encode/starlette.git", "encode/starlette"),
        ("https://github.com/encode/starlette/", "encode/starlette"),
    ],
)
def test_parse_github_url_accepts_public_repos(url, expected):
    assert parse_github_url(url) == (f"https://github.com/{expected}", expected)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://gitlab.com/a/b",
        "file:///etc/passwd",
        "https://github.com/a",
        "https://github.com/a/b/tree/main",
        "git@github.com:a/b.git",
        "https://github.com.evil.com/a/b",
    ],
)
def test_parse_github_url_rejects_everything_else(url):
    with pytest.raises(InvalidRepoUrl):
        parse_github_url(url)


@pytest.mark.parametrize(
    "path,ok",
    [
        ("pkg/mod.py", True),
        ("README.md", True),
        ("pyproject.toml", True),
        ("Dockerfile", True),
        ("node_modules/x/index.py", False),
        (".venv/lib/a.py", False),
        ("image.png", False),
        (".env", False),
        ("config/.env.production", False),
        ("certs/server.pem", False),
    ],
)
def test_is_indexable(path, ok):
    assert is_indexable(path) is ok


def test_keyword_terms_drop_stopwords_and_split_identifiers():
    terms = keyword_terms("How does url_path_for work in the Router?")
    assert terms[:2] == ["url_path_for", "url"]
    assert "router" in terms and "how" not in terms and "the" not in terms
    assert keyword_terms("how is it?") == []


def _hit(cid: int) -> SearchHit:
    return SearchHit(chunk_id=cid, path=f"f{cid}.py", symbol="s", kind="function", start_line=1, end_line=2, content="")


def test_rrf_rewards_agreement_between_retrievers():
    fused = rrf_fuse({"vector": [_hit(1), _hit(2), _hit(3)], "keyword": [_hit(3), _hit(4)]}, limit=4)
    assert fused[0].chunk_id == 3  # ranked by both
    assert fused[0].sources == ["vector", "keyword"]
    assert fused[1].chunk_id == 1  # a rank-1 hit beats any rank-2 hit
    assert {h.chunk_id for h in fused[2:]} == {2, 4}  # equal ranks, equal scores


def test_rrf_respects_limit_and_does_not_mutate_inputs():
    original = _hit(1)
    fused = rrf_fuse({"vector": [original], "keyword": [_hit(1)]}, limit=1)
    assert len(fused) == 1 and original.score == 0.0 and original.sources == []


def test_citations_valid_and_grounded():
    seen = SeenLines()
    seen.add("a.py", 10, 20)
    answer = "See [a.py:L12-L15], [a.py:L50-L55], [b.py:L1] and [missing.py:L1-L2]. Dup [a.py:L12-L15]."
    cites = {(c.path, c.start): c for c in check_citations(answer, {"a.py": 30, "b.py": 5}, seen)}
    assert len(cites) == 4
    assert cites[("a.py", 12)].valid and cites[("a.py", 12)].grounded
    assert not cites[("a.py", 50)].valid  # past end of file
    assert cites[("b.py", 1)].valid and not cites[("b.py", 1)].grounded  # real, but never shown
    assert not cites[("missing.py", 1)].valid


def test_repo_map_summarises_directories():
    text = repo_map(["README.md", "pkg/a.py", "pkg/sub/b.py", "pkg/sub/c.py"])
    assert "pkg/ (3 files)" in text and "pkg/sub/ (2 files)" in text and "README.md" in text


def test_citations_accept_monorepo_paths_with_at_signs():
    seen = SeenLines()
    seen.add("packages/@acme/ui/src/Button.tsx", 1, 40)
    answer = "Rendered in [packages/@acme/ui/src/Button.tsx:L10-L20]."
    (cite,) = check_citations(answer, {"packages/@acme/ui/src/Button.tsx": 50}, seen)
    assert cite.path == "packages/@acme/ui/src/Button.tsx" and cite.valid and cite.grounded
