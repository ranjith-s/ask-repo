# Decision notes

Short records of the choices made while building, with the alternatives considered.
These are working notes; the README explains the reasoning in prose.

| Area | Chosen | Considered | Why (short) |
|------|--------|------------|-------------|
| Scope | Python repos only | Multi-language via tree-sitter | Fits the time box; the `ast` module needs no extra dependency and gives exact line ranges. The chunker interface allows adding languages later. |
| Chunking | AST: function / method / class outline / module outline; windows for >120 lines; Markdown by heading | Fixed token windows; whole files | Retrieval units match how people ask ("how does X work"). Outlines answer "what's in this file". |
| Embeddings | `BAAI/bge-small-en-v1.5` via fastembed (ONNX, CPU) | OpenAI / Voyage APIs; `jina-embeddings-v2-base-code` | No GPU, no second API key, ~130 MB. A code-specific model is the first thing to try next, measured with `make eval`. |
| Vector DB | Postgres + pgvector | Chroma, Qdrant, Pinecone | One database for vectors, full-text search, files and logs. Hybrid search is a single SQL round trip each. Managed versions exist on every cloud. |
| Keyword search | Postgres full-text, `simple` config, symbol names weighted A, paths B | BM25 via Elasticsearch / `pg_search` | Good enough with weighting; no extra service. Measured: see `eval-log.md`. |
| Fusion | Reciprocal Rank Fusion (k=60) + test-file prior ×0.5 | Weighted score sum; cross-encoder reranker | RRF needs no score calibration between retrievers. A reranker is a next step. |
| Orchestration | ~150-line agent loop on the Anthropic SDK | LangGraph, LangChain, LlamaIndex, SDK tool runner | Every step is visible, testable with a scripted fake, and streamed to the UI. |
| LLM | Claude Opus 5.5, adaptive thinking, effort `medium` | Sonnet 5.5 for cost; Haiku for quick mode | Best tool-use quality; effort and model are env-configurable for cost tuning. |
| Agent tools | `search_code`, `grep`, `read_file`, `list_files` | AST-aware "find references", call graph | Mirrors how engineers explore code; grep covers exact identifiers that embeddings miss. |
| Modes | Deep (agent) and Quick (single retrieval) | Agent only | Quick is ~1 call: cheaper and faster, and it is the baseline the agent must beat. |
| Guardrails | Read-only tools; paths only resolved in DB; GitHub-only URLs; size limits; step cap with final no-tools turn; prompt-injection notice; citation validity + grounding check | LLM-judge on every answer | Deterministic checks are free and run on every answer. |
| Observability | structlog JSON + request IDs; `query_logs` table (steps, tokens, cost, latency) | OpenTelemetry / Langfuse | No extra service needed for the demo; OTel is the production path. |
| Frontend | React + Vite, served by FastAPI | Next.js | FastAPI owns the backend; one container, no CORS. |
| Ingestion jobs | FastAPI `BackgroundTasks` | Celery / SQS worker | Enough for a demo; production would move this to a queue. |
