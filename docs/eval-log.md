# Retrieval experiment log

Eval set: `backend/evals/starlette.yaml`: 20 answerable questions (plus 2 negative)
against `encode/starlette@0f08970e`. Metric: share of questions where an expected
file is in the top 5 (hit@5), plus mean reciprocal rank (MRR, top 10).

Run with `make eval`. Each row changed one thing.

> **Caveat:** the runs below used the offline `hash` embedder because the sandbox I
> built in could not download the embedding model. The **keyword** column is real;
> the **vector** column is noise from a stand-in embedder, so **hybrid** is a lower
> bound. Re-run with `EMBEDDING_BACKEND=fastembed` (the default) for real numbers.

| # | Change | keyword hit@5 / MRR | hybrid hit@5 / MRR |
|---|--------|--------------------|--------------------|
| 0 | Baseline: `ts_rank_cd`, unweighted `tsvector` | 0.30 / 0.17 | 0.30 / 0.20 |
| 1 | Symbol names weighted A, paths B; split punctuation so `routing.py` and `a.b-c` aren't single tokens; length normalization | 0.30 / 0.18 | 0.35 / 0.20 |
| 2 | Down-weight test files ×0.5 in keyword search | **0.55 / 0.40** | 0.40 / 0.28 |
| 3 | Same prior applied after RRF fusion | 0.55 / 0.40 | **0.50 / 0.31** |

What I looked at between runs: for each failing question, the top keyword hits.
Before #2, large test modules (`tests/test_responses.py` lists every test name in
its outline) took the top slots for almost every "how does X work" question.

Still failing (candidates for next steps): `background-tasks`, `testclient`,
`streaming-response`, `dependencies`, `lifespan`. These are concept questions where
the answer is spread across a class and its callers. The agent mode handles them by
searching more than once, which is the case for deep mode.

## JavaScript and TypeScript

Two eval sets were added alongside Starlette: `ky` (TypeScript) and `express` (JavaScript),
written against pinned commits. Same caveat as above: only the keyword column is a real
measurement in these runs.

| # | Change | Starlette kw | ky kw | Express kw |
|---|--------|------|------|------|
| 4 | JS/TS chunking with tree-sitter (baseline for the new sets) | 0.55 / 0.40 | 0.38 / 0.15 | 0.77 / 0.61 |
| 5 | Loose top-level code named by line (`@13`), not by module | **0.65 / 0.42** | n/m | n/m |
| 6 | Name words (`symbol_terms`) carry the top weight: camelCase split, none for tests and loose code; test-path rules extended to `__tests__/`, `*.test.ts`, `*.spec.js`, `test-d/` | **0.75 / 0.50** | **0.62 / 0.40** | **0.88 / 0.78** |

(hit@5 / MRR; n/m: not measured separately, #5 and #6 were measured together on the new sets.)

What the misses showed before #6:
- In JS, tests read `test('retries without the computed delay', …)`. Naming chunks after that
  string put a natural-language title in the highest-weighted field, so tests outranked the
  implementation for "how does X work" questions.
- Postgres splits `snake_case` into words but not `camelCase`, so `normalizeRequestMethod`
  was one opaque token: TypeScript names got almost none of the name boost Python names get.
- A module-name label on leftover top-level code (`starlette.routing:12`) matched any question
  mentioning the file name, ranking `module.exports = router` above the actual route.

Still missing on ky: "retried" vs "retry" (no stemming in the `simple` text search config)
and questions whose answer lives in one large class (`Ky.ts`, 1,400 lines). Both are cases
the real embedding model should help with; re-run with `EMBEDDING_BACKEND=fastembed`.
