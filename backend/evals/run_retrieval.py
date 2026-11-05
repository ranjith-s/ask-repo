"""Retrieval eval: does the right file show up in the top-k? No LLM, no API key.

Compares three strategies on the same index:
  vector   - pgvector cosine similarity only
  keyword  - weighted Postgres full-text search only
  hybrid   - both, fused with reciprocal rank fusion (what the app uses)

Metrics per strategy:
  hit@k  - share of questions with at least one expected file in the top k
  MRR    - mean of 1/rank of the first expected file (top 10), 0 if absent

Usage:  uv run python -m evals.run_retrieval [--k 5] [--embedding hash] [--reindex]
"""

import argparse
import json
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

from app.config import Settings
from app.models import SearchHit
from evals.common import EVALS_DIR, index_eval_repo, load_eval_set

DEPTH = 10


def first_relevant_rank(hits: list[SearchHit], expected: list[str]) -> int | None:
    for rank, hit in enumerate(hits, start=1):
        if hit.path in expected:
            return rank
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--embedding", choices=["fastembed", "hash"], default=None)
    parser.add_argument("--reindex", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    if args.embedding:
        settings = settings.model_copy(update={"embedding_backend": args.embedding})
    eval_set = load_eval_set()
    ctx = index_eval_repo(eval_set, settings, reindex=args.reindex)
    repo_id, store, embedder = ctx.repo["id"], ctx.store, ctx.embedder

    strategies: dict[str, Callable[[str], list[SearchHit]]] = {
        "vector": lambda q: store.vector_search(repo_id, embedder.embed_query(q), DEPTH),
        "keyword": lambda q: store.keyword_search(repo_id, q, DEPTH),
        "hybrid": lambda q: store.hybrid_search(repo_id, q, embedder.embed_query(q), DEPTH),
    }
    questions = [q for q in eval_set.questions if q.expected_files]
    per_question = []
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"hits": 0.0, "rr": 0.0})
    by_category: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for q in questions:
        row: dict[str, object] = {"id": q.id, "category": q.category}
        for name, search in strategies.items():
            rank = first_relevant_rank(search(q.question), q.expected_files)
            hit = rank is not None and rank <= args.k
            totals[name]["hits"] += hit
            totals[name]["rr"] += 1 / rank if rank else 0.0
            by_category[q.category][name].append(float(hit))
            row[name] = rank
        per_question.append(row)

    n = len(questions)
    summary = {
        name: {f"hit@{args.k}": round(t["hits"] / n, 3), "mrr": round(t["rr"] / n, 3)} for name, t in totals.items()
    }

    print(f"\nRetrieval eval: {eval_set.repo}@{eval_set.commit[:8]}, {n} questions")
    print(f"embedding backend: {settings.embedding_backend}")
    print(f"{'strategy':<10}{'hit@' + str(args.k):>8}{'MRR':>8}")
    for name, s in summary.items():
        print(f"{name:<10}{s[f'hit@{args.k}']:>8.2f}{s['mrr']:>8.2f}")
    print(f"\nhit@{args.k} by category")
    print(f"{'category':<10}" + "".join(f"{s:>9}" for s in strategies))
    for cat, vals in sorted(by_category.items()):
        print(f"{cat:<10}" + "".join(f"{sum(vals[s]) / len(vals[s]):>9.2f}" for s in strategies))
    print("\nRank of first expected file per question (- = not in top 10)")
    for row in per_question:
        print(f"  {row['id']:<20}" + "".join(f"{(row[s] or '-')!s:>9}" for s in strategies))

    out = EVALS_DIR / "results" / f"retrieval-{settings.embedding_backend}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "run_at": datetime.now(UTC).isoformat(),
                "repo": eval_set.repo,
                "commit": eval_set.commit,
                "embedding": settings.embedding_backend,
                "k": args.k,
                "summary": summary,
                "questions": per_question,
            },
            indent=2,
        )
    )
    print(f"\nSaved {out.relative_to(EVALS_DIR.parent)}")
    store.pool.close()


if __name__ == "__main__":
    main()
