"""Answer eval: run the real agent on every question and score the answers.

Needs ANTHROPIC_API_KEY and costs real money (roughly $0.03-0.10 per question
in deep mode at current Opus 5.5 prices; check the printed total).

Scored automatically, no LLM judge:
  answered        - finished without an error
  cites_expected  - at least one citation points at an expected file
  valid_cites     - share of citations whose file and line range exist
  grounded_cites  - share of citations the model was actually shown
  declines        - for negative questions: says it isn't in the repo, with no citations
Plus steps, latency and cost per question.

Usage:  uv run python -m evals.run_answers [--mode deep|quick] [--only id1,id2]
"""

import argparse
import asyncio
import json
import re
import statistics
from datetime import UTC, datetime
from typing import Any

from app.agent.llm import AnthropicLLM
from app.agent.runner import QuestionAnswerer
from app.config import Settings
from evals.common import EVALS_DIR, index_eval_repo, load_eval_set

DECLINE = re.compile(
    r"\b(does not|doesn't|do not|don't|no)\b.{0,60}\b(contain|include|implement|use|support|provide|find|mention)",
    re.I,
)


async def run(mode: str, only: set[str] | None) -> None:
    settings = Settings()
    if not settings.anthropic_api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY to run the answer eval.")
    eval_set = load_eval_set()
    ctx = index_eval_repo(eval_set, settings)
    answerer = QuestionAnswerer(ctx.store, ctx.embedder, AnthropicLLM(settings), settings)

    rows: list[dict[str, Any]] = []
    for q in eval_set.questions:
        if only and q.id not in only:
            continue
        text, done, error, steps = "", None, None, 0
        async for event in answerer.answer(ctx.repo, q.question, [], mode):  # type: ignore[arg-type]
            if event["type"] == "text":
                text += event["delta"]
            elif event["type"] == "step":
                steps += 1
                text = ""  # text before a tool call is narration, not the answer
            elif event["type"] == "done":
                done = event
            elif event["type"] == "error":
                error = event["message"]
        cites = done["citations"] if done else []
        row: dict[str, Any] = {
            "id": q.id,
            "category": q.category,
            "answered": error is None,
            "error": error,
            "steps": steps,
            "citations": len(cites),
            "valid_cites": _share(cites, "valid"),
            "grounded_cites": _share(cites, "grounded"),
            "latency_ms": done["latency_ms"] if done else None,
            "cost_usd": done["usage"]["cost_usd"] if done else 0.0,
            "answer": text.strip(),
        }
        if q.expected_files:
            row["cites_expected"] = any(c["path"] in q.expected_files for c in cites)
        else:
            row["declines"] = not cites and bool(DECLINE.search(text))
        rows.append(row)
        print(
            f"  {q.id:<20} steps={steps:<2} cites={len(cites):<2} "
            f"{'expected✓' if row.get('cites_expected') or row.get('declines') else 'expected✗'} "
            f"{row['latency_ms'] or 0:>6}ms ${row['cost_usd']:.4f}" + (f"  ERROR {error}" if error else "")
        )

    positives = [r for r in rows if "cites_expected" in r]
    negatives = [r for r in rows if "declines" in r]
    summary = {
        "mode": mode,
        "questions": len(rows),
        "answered": _rate(rows, "answered"),
        "cites_expected": _rate(positives, "cites_expected"),
        "negatives_declined": _rate(negatives, "declines"),
        "valid_cites": _mean([r["valid_cites"] for r in rows if r["citations"]]),
        "grounded_cites": _mean([r["grounded_cites"] for r in rows if r["citations"]]),
        "median_latency_ms": statistics.median([r["latency_ms"] for r in rows if r["latency_ms"]] or [0]),
        "avg_steps": _mean([r["steps"] for r in rows]),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
    }
    print("\n" + "\n".join(f"{k:<20}{v}" for k, v in summary.items()))
    out = EVALS_DIR / "results" / f"answers-{mode}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "run_at": datetime.now(UTC).isoformat(),
                "model": settings.llm_model,
                "effort": settings.llm_effort,
                "summary": summary,
                "rows": rows,
            },
            indent=2,
        )
    )
    print(f"Saved {out.relative_to(EVALS_DIR.parent)}")


def _share(cites: list[dict[str, Any]], key: str) -> float:
    return round(sum(c[key] for c in cites) / len(cites), 3) if cites else 0.0


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    return round(sum(bool(r[key]) for r in rows) / len(rows), 3) if rows else 0.0


def _mean(values: list[float]) -> float:
    return round(statistics.mean(values), 3) if values else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["deep", "quick"], default="deep")
    parser.add_argument("--only", default="", help="comma-separated question ids")
    args = parser.parse_args()
    asyncio.run(run(args.mode, set(args.only.split(",")) if args.only else None))


if __name__ == "__main__":
    main()
