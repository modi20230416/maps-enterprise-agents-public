"""Local API. UI is served separately, and credentials stay in worker environment."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from enterprise_agent.application.task_runtime import TaskRuntime
from enterprise_agent.domain.contracts import MapsEvent
from enterprise_agent.domain.runtime import TaskConflict, TERMINAL
from enterprise_agent.infrastructure.llm import LLMConfig
from enterprise_agent.infrastructure.mes import InMemoryMES, parse_baseline
from enterprise_agent.infrastructure.task_store import TaskStore, digest

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = ROOT / "examples/orchestrate_llm.json"


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["orchestrate.monitor"] = "orchestrate.monitor"
    mode: Literal["llm", "protocol_fixture"] = "llm"
    scenario: Literal["factory-progress"] = "factory-progress"
    input: dict | None = None
    timeout_seconds: float = Field(default=120, ge=1, le=600)
    max_attempts: int = Field(default=2, ge=1, le=3)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]+$")
    decision: Literal["APPROVED", "REJECTED"]
    reviewer: str = Field(min_length=1, max_length=80)
    comment: str = Field(default="", max_length=1000)


def validate_input(data):
    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"api_key", "apikey", "authorization", "password", "secret", "access_token", "maps_llm_api_key"}:
                    raise ValueError("credentials belong in backend environment")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(data)
    if not isinstance(data, dict) or not set(data) <= {"description", "mes", "event"}:
        raise ValueError("expected mes and event input")
    InMemoryMES(parse_baseline(data["mes"]))
    MapsEvent.parse(data["event"])
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 256_000:
        raise ValueError("input too large")


def llm_ready():
    try:
        LLMConfig.from_env()
        return True
    except (ValueError, OSError):
        return False


def create_app(db_path="artifacts/platform.sqlite3", *, workers=2, allow_protocol_fixture=False,
               start_workers=True, origins=None):
    allowed_origins = origins or ["http://127.0.0.1:5173", "http://localhost:5173"]
    store = TaskStore(db_path)
    runtime = TaskRuntime(store, workers=workers)

    @asynccontextmanager
    async def lifespan(app):
        if start_workers:
            runtime.start()
        yield
        if start_workers:
            await asyncio.to_thread(runtime.stop)

    app = FastAPI(title="MAPS Local Task API", version="1.0.0", lifespan=lifespan)
    app.state.store, app.state.runtime = store, runtime
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def request_boundary(request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
            if request.url.path.endswith(("/tasks", "/reviews/orchestrate", "/reviews/planning", "/reviews/scheduling", "/reviews/warehouse")):
                if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    return JSONResponse({"detail": "application/json required"}, status_code=415)
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 300_000:
                    return JSONResponse({"detail": "request too large"}, status_code=413)
                chunks.append(chunk)
            # Starlette's cached middleware request replays _body to the parser.
            request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(TaskConflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "task not found"}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"detail": "invalid request fields"}, status_code=422)

    @app.get("/api/health")
    def health():
        return {"schema_version": 1, "llm_configured": llm_ready(), "protocol_fixture_allowed": allow_protocol_fixture,
                "worker_count": workers, "runtime_healthy": runtime.healthy,
                "runtime_scope": "same_host", "execution_authorized": False}

    @app.get("/api/scenarios")
    def scenarios():
        return [{"id": "factory-progress", "name": "加工进度偏慢 · 前瞻与按需初始化", "synthetic": True,
                 "input": json.loads(SCENARIO.read_text(encoding="utf-8-sig"))}]

    @app.post("/api/tasks", status_code=202)
    def submit(body: TaskRequest, idempotency_key: str | None = Header(default=None, max_length=100)):
        data = body.input if body.input is not None else json.loads(SCENARIO.read_text(encoding="utf-8-sig"))
        try:
            validate_input(data)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise HTTPException(422, "invalid factory input; nothing queued") from None
        if body.mode == "protocol_fixture":
            if not allow_protocol_fixture:
                raise HTTPException(403, "protocol fixture is disabled")
            if digest(data) != digest(json.loads(SCENARIO.read_text(encoding="utf-8-sig"))):
                raise HTTPException(422, "protocol fixture only supports the bundled synthetic scenario")
        elif not llm_ready():
            raise HTTPException(503, "configure LLM in backend environment before submitting llm tasks")
        task, created = store.submit(data, kind=body.kind, mode=body.mode, request_key=idempotency_key,
                                     timeout_seconds=body.timeout_seconds, max_attempts=body.max_attempts)
        return {"task_id": task["id"], "created": created, "status": task["status"]}

    @app.get("/api/tasks")
    def tasks(limit: int = Query(default=100, ge=1, le=200)):
        return store.list(limit)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return store.get(task_id)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        store.cancel(task_id)
        return {"status": "CANCELLED"}

    @app.post("/api/tasks/{task_id}/rerun", status_code=202)
    def rerun(task_id: str):
        prior = store.get(task_id)
        if prior["mode"] == "protocol_fixture" and not allow_protocol_fixture:
            raise HTTPException(403, "protocol fixture is disabled")
        if prior["mode"] == "llm" and not llm_ready():
            raise HTTPException(503, "LLM not configured")
        task, _ = store.submit(prior["input"], mode=prior["mode"], kind=prior["kind"], parent_id=task_id,
                               timeout_seconds=prior["timeout_seconds"], max_attempts=prior["max_attempts"])
        return {"task_id": task["id"], "status": task["status"]}

    @app.post("/api/tasks/{task_id}/reviews/{module}")
    def review(task_id: str, module: str, body: ReviewRequest):
        if module not in {"orchestrate", "planning", "scheduling", "warehouse"} or not body.reviewer.strip():
            raise HTTPException(422, "invalid module or reviewer")
        store.review(task_id, module, body.binding, body.decision, body.reviewer, body.comment)
        return store.get(task_id)

    @app.get("/api/tasks/{task_id}/events")
    def events(task_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=1000)):
        return store.events(task_id, after, limit)

    @app.get("/api/tasks/{task_id}/replay")
    def replay(task_id: str, upto: int | None = Query(default=None, ge=0)):
        return store.replay(task_id, upto)

    @app.get("/api/tasks/{task_id}/events/stream")
    async def stream(task_id: str, request: Request, after: int = Query(default=0, ge=0),
                     last_event_id: str | None = Header(default=None)):
        store.get(task_id)
        try:
            cursor = max(after, int(last_event_id or 0))
        except ValueError:
            raise HTTPException(422, "invalid event cursor") from None

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(store.events, task_id, cursor)
                for event in batch:
                    cursor = event["cursor"]
                    yield f"id: {cursor}\nevent: task-event\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                current = await asyncio.to_thread(store.get, task_id)
                if not batch and current["status"] in TERMINAL | {"WAITING_REVIEW"}:
                    break
                if not batch:
                    yield ": waiting\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app
