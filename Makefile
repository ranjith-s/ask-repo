.PHONY: up down setup db dev api web test lint fmt eval eval-answers

up:            ## Build and run everything at http://localhost:8000
	docker compose up --build

down:
	docker compose down

setup:         ## Install backend and frontend dependencies (needed once without Docker)
	cd backend && uv sync
	cd frontend && npm install

# Uses a Postgres already listening on localhost:5432 (e.g. a native install) if there
# is one; otherwise starts the Docker one. Override the port check with DB_HOST/DB_PORT.
DB_HOST ?= localhost
DB_PORT ?= 5432
db:            ## Ensure Postgres + pgvector is running (native if found, else Docker)
	@if (command -v pg_isready >/dev/null 2>&1 && pg_isready -q -h $(DB_HOST) -p $(DB_PORT)) \
	   || (cd backend && uv run python -c "import socket; socket.create_connection(('$(DB_HOST)', $(DB_PORT)), 2)" >/dev/null 2>&1); then \
		echo "Postgres is already running on $(DB_HOST):$(DB_PORT); not starting Docker."; \
	elif command -v docker >/dev/null 2>&1; then \
		docker compose up -d --wait db; \
	else \
		echo "No Postgres on $(DB_HOST):$(DB_PORT) and Docker is not installed."; \
		echo "Install Postgres 16 + pgvector (see README: Running without Docker)."; \
		exit 1; \
	fi

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

eval-answers: db  ## Answer eval on the configured LLM provider (API providers cost money)
	cd backend && uv run python -m evals.run_answers
