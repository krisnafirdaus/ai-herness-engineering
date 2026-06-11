"""FastAPI control plane.

Endpoints:
    POST   /runs                 enqueue a new run            -> {run_id, status}
    GET    /runs                 list runs
    GET    /runs/{id}            run + step state
    GET    /runs/{id}/stream     LIVE run progress (Server-Sent Events)
    WS     /runs/{id}/ws         LIVE run progress (WebSocket)
    GET    /runs/{id}/traces     token/latency telemetry
    GET    /runs/{id}/events     run event log (static)
    GET    /runs/{id}/diff       git diff vs base
    POST   /runs/{id}/pr         push branch + open the GitHub PR
    POST   /runs/{id}/resume     re-enqueue a stuck run
    GET    /healthz

Real-time visibility is **push-based**: every persisted run event is published
to the event bus (in-process or Redis pub/sub) and fanned out to SSE/WebSocket
subscribers as it happens. The events table doubles as the replay log, so SSE
reconnects resume losslessly from ``Last-Event-ID`` and a quiet bus is
backstopped by a durable-log catch-up — clients never need to poll.

In ``redis`` queue mode the run id is pushed to the broker for a worker pool to
pick up. In single-node ``db`` mode the API drives the run in a FastAPI
background task, so the API is usable without a separate worker process.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import (BackgroundTasks, FastAPI, HTTPException, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..events import get_bus
from ..git import RepoManager
from ..orchestrator.states import RunState
from ..queue import get_queue
from ..storage.models import Repository
from ..telemetry.tracing import Tracer

app = FastAPI(title="AI Agent Harness", version="0.1.0")

# Streaming tunables (seconds).
_STREAM_WAIT_SLICE = 1.0    # bus wait per iteration
_STREAM_HEARTBEAT = 10.0    # SSE comment heartbeat when idle
_STREAM_END_GRACE = 2.0     # linger after terminal state for trailing events


class CreateRun(BaseModel):
    repo: str
    task: str
    branch: str | None = None


def _drive_bg(run_id: str) -> None:
    # Imported lazily so importing the app doesn't pull the whole orchestrator.
    from ..orchestrator.state_machine import StateMachine

    StateMachine().drive(run_id)


def _serialize_run(repo: Repository, run) -> dict:
    return {
        "run_id": run.run_id,
        "task": run.task,
        "repo": run.repo_url,
        "status": run.status,
        "branch": run.branch,
        "current_step": run.current_step,
        "total_steps": run.total_steps,
        "tokens_used": run.tokens_used,
        "token_budget": settings.max_tokens_per_run,
        "error": run.error,
        "pr_url": run.pr_url,
        "steps": [
            {"step_id": s.step_id, "file": s.file, "action": s.action,
             "status": s.status, "iterations": s.iterations, "reason": s.reason}
            for s in repo.get_steps(run.run_id)
        ],
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "queue": settings.queue, "sandbox": settings.sandbox,
            "provider": settings.llm_provider}


@app.post("/runs", status_code=202)
def create_run(body: CreateRun, background: BackgroundTasks) -> dict:
    repo = Repository()
    run = repo.create_run(body.repo, body.task, body.branch or "harness/auto")
    if settings.queue == "redis":
        get_queue().enqueue(run.run_id)  # a worker pool will drive it
    else:
        background.add_task(_drive_bg, run.run_id)  # single-node: drive in-process
    return {"run_id": run.run_id, "status": run.status}


@app.get("/runs")
def list_runs(status: str | None = None) -> dict:
    repo = Repository()
    return {"runs": [{"run_id": r.run_id, "status": r.status, "task": r.task}
                     for r in repo.list_runs(status)]}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    repo = Repository()
    run = repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return _serialize_run(repo, run)


def _sse(ev: dict) -> str:
    return f"id: {ev['id']}\nevent: run-event\ndata: {json.dumps(ev)}\n\n"


def _event_stream(run_id: str, last_event_id: int):
    """SSE generator: snapshot -> replay -> live push (deduped by event id).

    Subscribes to the bus BEFORE querying the replay log so no event can fall
    between the two. When the bus is quiet the durable log is used as a
    catch-up source (covers cross-process deployments without Redis); the
    stream closes itself shortly after the run reaches a terminal state.
    """
    repo = Repository()
    sub = get_bus().subscribe(run_id)
    try:
        run = repo.get_run(run_id)
        yield ("event: snapshot\ndata: "
               + json.dumps(_serialize_run(repo, run)) + "\n\n")

        last_sent = last_event_id
        for ev in repo.get_events_since(run_id, last_sent):
            last_sent = ev["id"]
            yield _sse(ev)

        idle = 0.0
        since_heartbeat = 0.0
        while True:
            ev = sub.get(timeout=_STREAM_WAIT_SLICE)
            if ev is not None and ev.get("id", 0) > last_sent:
                last_sent = ev["id"]
                idle = since_heartbeat = 0.0
                yield _sse(ev)
                continue
            if ev is not None:
                continue  # duplicate of something already replayed

            idle += _STREAM_WAIT_SLICE
            since_heartbeat += _STREAM_WAIT_SLICE
            # Bus quiet: catch up from the durable log (resilience path).
            fresh = repo.get_events_since(run_id, last_sent)
            if fresh:
                for ev in fresh:
                    last_sent = ev["id"]
                    yield _sse(ev)
                idle = since_heartbeat = 0.0
                continue
            run = repo.get_run(run_id)
            if run and RunState(run.status).is_terminal and idle >= _STREAM_END_GRACE:
                yield ("event: end\ndata: "
                       + json.dumps({"run_id": run_id, "status": run.status})
                       + "\n\n")
                return
            if since_heartbeat >= _STREAM_HEARTBEAT:
                since_heartbeat = 0.0
                yield ": keep-alive\n\n"
    finally:
        sub.close()


@app.get("/runs/{run_id}/stream")
def stream_run(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """Live run progress as Server-Sent Events (no client polling).

    Reconnect support: browsers send ``Last-Event-ID`` automatically; manual
    clients may pass ``?after=<event_id>``. Replay starts after that id.
    """
    if not Repository().get_run(run_id):
        raise HTTPException(404, "run not found")
    last_id = after or int(request.headers.get("last-event-id") or 0)
    return StreamingResponse(
        _event_stream(run_id, last_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@app.websocket("/runs/{run_id}/ws")
async def stream_run_ws(websocket: WebSocket, run_id: str) -> None:
    """Live run progress over a WebSocket (same merge logic as SSE)."""
    await websocket.accept()
    loop = asyncio.get_running_loop()
    repo = Repository()
    run = await loop.run_in_executor(None, repo.get_run, run_id)
    if not run:
        await websocket.send_json({"type": "error", "detail": "run not found"})
        await websocket.close()
        return

    sub = get_bus().subscribe(run_id)
    try:
        await websocket.send_json({"type": "snapshot",
                                   "run": _serialize_run(repo, run)})
        last_sent = 0
        for ev in await loop.run_in_executor(
                None, repo.get_events_since, run_id, last_sent):
            last_sent = ev["id"]
            await websocket.send_json({"type": "event", **ev})

        idle = 0.0
        while True:
            ev = await loop.run_in_executor(None, sub.get, _STREAM_WAIT_SLICE)
            if ev is not None and ev.get("id", 0) > last_sent:
                last_sent = ev["id"]
                idle = 0.0
                await websocket.send_json({"type": "event", **ev})
                continue
            if ev is not None:
                continue
            idle += _STREAM_WAIT_SLICE
            fresh = await loop.run_in_executor(
                None, repo.get_events_since, run_id, last_sent)
            if fresh:
                for ev in fresh:
                    last_sent = ev["id"]
                    await websocket.send_json({"type": "event", **ev})
                idle = 0.0
                continue
            run = await loop.run_in_executor(None, repo.get_run, run_id)
            if run and RunState(run.status).is_terminal and idle >= _STREAM_END_GRACE:
                await websocket.send_json({"type": "end", "status": run.status})
                return
    except WebSocketDisconnect:
        pass
    finally:
        sub.close()


@app.get("/runs/{run_id}/traces")
def get_traces(run_id: str) -> dict:
    repo = Repository()
    rows = repo.get_telemetry(run_id)
    return {"summary": Tracer.summarize(rows), "spans": rows}


@app.get("/runs/{run_id}/events")
def get_events(run_id: str) -> dict:
    return {"events": Repository().get_events(run_id)}


@app.get("/runs/{run_id}/diff")
def get_diff(run_id: str) -> dict:
    repo = Repository()
    run = repo.get_run(run_id)
    if not run or not run.workspace_path:
        raise HTTPException(404, "no workspace for run")
    return {"diff": RepoManager(run.workspace_path).diff(run.base_ref)}


@app.post("/runs/{run_id}/pr")
def create_pr(run_id: str) -> dict:
    """Push the run branch and open (or reuse) the GitHub pull request."""
    from ..git.github import GitHubError, create_pr_for_run

    repo = Repository()
    run = repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    try:
        url = create_pr_for_run(repo, run)
    except GitHubError as exc:
        raise HTTPException(409, str(exc))
    return {"run_id": run_id, "pr_url": url}


@app.post("/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: str, background: BackgroundTasks) -> dict:
    repo = Repository()
    run = repo.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if settings.queue == "redis":
        get_queue().enqueue(run_id)
    else:
        background.add_task(_drive_bg, run_id)
    return {"run_id": run_id, "status": run.status, "resuming": True}
