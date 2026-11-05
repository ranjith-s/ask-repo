"""App factory: wires settings, DB, embedder, LLM and routes together."""

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.llm import LLM, AnthropicLLM
from app.agent.runner import QuestionAnswerer
from app.api import router
from app.config import Settings, get_settings
from app.db import apply_schema, create_pool
from app.embeddings import Embedder, create_embedder
from app.ingest.pipeline import IngestionPipeline
from app.observability import configure_logging, log
from app.store import RepoStore


def create_app(settings: Settings | None = None, embedder: Embedder | None = None, llm: LLM | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(settings.database_url)
        apply_schema(pool, settings.embedding_dim)
        store = RepoStore(pool)
        emb = embedder or create_embedder(settings.embedding_backend, settings.embedding_model, settings.embedding_dim)
        app.state.settings = settings
        app.state.store = store
        app.state.pipeline = IngestionPipeline(store, emb, settings)
        app.state.answerer = QuestionAnswerer(store, emb, llm or AnthropicLLM(settings), settings)
        log.info("app.started", model=settings.llm_model, embedding=settings.embedding_backend)
        yield
        pool.close()

    app = FastAPI(title="Ask Repo", lifespan=lifespan)
    app.include_router(router)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        if request.url.path.startswith("/api") and request.url.path != "/api/health":
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        structlog.contextvars.clear_contextvars()
        return response

    _mount_frontend(app, Path(settings.static_dir))
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the built React app from the same origin, so there is one container and no CORS."""
    if not (static_dir / "index.html").exists():
        return
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = (static_dir / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
            return FileResponse(candidate)
        return FileResponse(static_dir / "index.html")
