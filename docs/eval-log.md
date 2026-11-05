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
