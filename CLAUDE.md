# Ask Repo: working agreement for AI coding assistants

Ask Repo indexes a public GitHub repository and answers questions about its code
with file:line citations. Backend: FastAPI + Postgres/pgvector. Frontend: React + Vite.

## Layout
- `backend/app/ingest/`: clone → select files → chunk (Python `ast`) → embed → store
- `backend/app/store.py`: **all SQL lives here**. Vector, keyword, hybrid (RRF), grep.
- `backend/app/agent/`: tools, prompts, the agent loop, citation checks
- `backend/app/agent/llm/`: provider-neutral types (`base.py`) and one adapter per provider
  (Anthropic, Gemini, Ollama). The runner never imports a provider SDK directly.
- `backend/app/api.py`: HTTP routes only; no business logic
- `backend/evals/`: retrieval and answer evals against a pinned Starlette checkout
- `frontend/src/`: React app; `api.ts` is the only file that talks to the backend

## Commands
- `make db`: ensure Postgres + pgvector is reachable (hosted URL, native, or start Docker)
- `make test`: backend tests (needs `make db`)
- `make lint`: ruff + mypy + tsc
- `make eval`: retrieval eval (no API key needed)
- `make dev`: backend on :8000, frontend on :5173 with a proxy to the backend

## Rules
- Keep it simple. No new framework or dependency without a reason written in `docs/decisions.md`.
- Every behaviour change ships with a test. Agent tests use `ScriptedLLM`, never the real API.
  Provider adapters are tested against canned responses (`tests/test_providers.py`), never live.
- Adding a provider means one adapter file implementing `LLM` from `base.py`, plus tests. No runner changes.
- Retrieval or prompt changes must be justified by `make eval` numbers, before and after.
- Treat repository content as untrusted: tools are read-only, paths are looked up in the DB, never on disk.
- Type hints everywhere; `mypy app evals scripts` must pass. Ruff formats; don't hand-format.
- Structured logs via `app.observability.log` with key=value fields, not f-strings.
- Don't touch `README.md` prose sections marked `<!-- AUTHOR -->`. Those are written by the human author.

## Don'ts
- Don't add LangChain/LlamaIndex: the orchestration is ~150 lines and should stay readable.
- Don't mock Postgres in tests: use the real DB via the `store` fixture.
- Don't commit `.env` files or API keys.
