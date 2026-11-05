"""HTTP routes. Thin: validate input, delegate, shape the response."""

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.runner import HistoryTurn, QuestionAnswerer
from app.config import Settings
from app.ingest.pipeline import IngestionPipeline
from app.ingest.source import InvalidRepoUrl, parse_github_url
from app.store import RepoStore

router = APIRouter(prefix="/api")

# Repos in these states are still being indexed.
BUSY = {"pending", "cloning", "parsing", "embedding"}


def get_store(request: Request) -> RepoStore:
    return request.app.state.store  # type: ignore[no-any-return]


StoreDep = Annotated[RepoStore, Depends(get_store)]


class IndexRequest(BaseModel):
    url: str = Field(max_length=300)


class HistoryItem(BaseModel):
    question: str = Field(max_length=4000)
    answer: str = Field(max_length=20000)


class AskRequest(BaseModel):
    question: str = Field(min_length=3)
    mode: Literal["quick", "deep"] = "deep"
    history: list[HistoryItem] = Field(default_factory=list, max_length=20)


def _public(repo: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in repo.items()}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
def public_config(request: Request) -> dict[str, Any]:
    s: Settings = request.app.state.settings
    return {
        "provider": s.llm_provider,
        "model": s.model,
        "llm_configured": s.llm_problem() is None,
        "llm_problem": s.llm_problem(),
        "embedding_model": s.embedding_model,
    }


@router.get("/repos")
def list_repos(store: StoreDep) -> list[dict[str, Any]]:
    return [_public(r) for r in store.list_repos()]


@router.post("/repos", status_code=202)
def index_repo(body: IndexRequest, background: BackgroundTasks, request: Request, store: StoreDep) -> dict[str, Any]:
    try:
        url, name = parse_github_url(body.url)
    except InvalidRepoUrl as exc:
        raise HTTPException(422, str(exc)) from exc
    existing = store.find_repo_by_url(url)
    if existing and existing["status"] in BUSY:
        return _public(existing)  # already in progress; don't start a second run
    repo_id = store.upsert_repo(url, name)
    pipeline: IngestionPipeline = request.app.state.pipeline
    background.add_task(pipeline.run, repo_id, url)
    repo = store.get_repo(repo_id)
    assert repo is not None
    return _public(repo)


@router.get("/repos/{repo_id}/file")
def get_file(repo_id: int, path: str, store: StoreDep) -> dict[str, Any]:
    content = store.get_file(repo_id, path)
    if content is None:
        raise HTTPException(404, "File not found")
    return {"path": path, "content": content}


@router.post("/repos/{repo_id}/ask")
async def ask(repo_id: int, body: AskRequest, request: Request, store: StoreDep) -> StreamingResponse:
    settings: Settings = request.app.state.settings
    if problem := settings.llm_problem():
        raise HTTPException(503, f"LLM not configured: {problem}")
    if len(body.question) > settings.question_max_chars:
        raise HTTPException(422, f"Question is too long (max {settings.question_max_chars} characters)")
    repo = store.get_repo(repo_id)
    if repo is None:
        raise HTTPException(404, "Repository not found")
    if repo["status"] != "ready":
        raise HTTPException(409, f"Repository is not ready (status: {repo['status']})")

    answerer: QuestionAnswerer = request.app.state.answerer
    history = [HistoryTurn(h.question, h.answer) for h in body.history]

    async def sse() -> AsyncIterator[str]:
        async for event in answerer.answer(repo, body.question.strip(), history, body.mode):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
