.PHONY: up down setup db test-db dev api web test lint fmt eval eval-answers

up:            ## Build and run everything at http://localhost:8000
	docker compose up --build

down:
	docker compose down

setup:         ## Install backend and frontend dependencies (needed once without Docker)
	cd backend && uv sync
	cd frontend && npm install

# Ensures the database in DATABASE_URL is reachable: a hosted one (Neon, Supabase…) is
# only checked; a local one is used if running, otherwise the Docker one is started.
db:            ## Ensure Postgres + pgvector is reachable (hosted, native, or Docker)
	@cd backend && uv run --quiet python -m scripts.ensure_db

# Tests always use their own database, local by default, never your DATABASE_URL.
TEST_DATABASE_URL ?= postgresql://askrepo:askrepo@localhost:5432/askrepo_test
test-db:
	@cd backend && uv run --quiet python -m scripts.ensure_db --url "$(TEST_DATABASE_URL)"

api:           ## Backend with reload on :8000
	cd backend && uv run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

web:           ## Frontend dev server on :5173 (proxies /api to :8000)
	cd frontend && npm run dev

dev: db        ## Backend and frontend together
	$(MAKE) -j2 api web

test: test-db  ## Backend tests against a real Postgres (TEST_DATABASE_URL)
	cd backend && TEST_DATABASE_URL="$(TEST_DATABASE_URL)" uv run pytest -q

lint:          ## Ruff, mypy and the TypeScript compiler
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app evals scripts
	cd frontend && npx tsc -b

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .

eval: db       ## Retrieval eval (no API key needed)
	cd backend && uv run python -m evals.run_retrieval

eval-answers: db  ## Answer eval on the configured LLM provider (API providers cost money)
	cd backend && uv run python -m evals.run_answers
