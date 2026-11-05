.PHONY: up down db dev api web test lint fmt eval eval-answers

up:            ## Build and run everything at http://localhost:8000
	docker compose up --build

down:
	docker compose down

db:            ## Start only Postgres (for local dev and tests)
	docker compose up -d db

api:           ## Backend with reload on :8000
	cd backend && uv run uvicorn app.main:create_app --factory --reload --port 8000

web:           ## Frontend dev server on :5173 (proxies /api to :8000)
	cd frontend && npm run dev

dev: db        ## Backend and frontend together
	$(MAKE) -j2 api web

test: db       ## Backend tests against a real Postgres
	cd backend && uv run pytest -q

lint:          ## Ruff, mypy and the TypeScript compiler
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app evals
	cd frontend && npx tsc -b

fmt:
	cd backend && uv run ruff check --fix . && uv run ruff format .

eval: db       ## Retrieval eval (no API key needed)
	cd backend && uv run python -m evals.run_retrieval

eval-answers: db  ## Answer eval on the real model (needs ANTHROPIC_API_KEY, costs money)
	cd backend && uv run python -m evals.run_answers
