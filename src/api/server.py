"""FastAPI control plane.

Endpoints:
    POST   /runs                 enqueue a new run            -> {run_id, status}
    GET    /runs                 list runs
    GET    /runs/{id}            run + step state
    GET    /runs/{id}/traces     token/latency telemetry
    GET    /runs/{id}/events     run event log
    GET    /runs/{id}/diff       git diff vs base
    POST   /runs/{id}/resume     re-enqueue a stuck run
    GET    /healthz

In ``redis`` queue mode the run id is pushed to the broker for a worker pool to
pick up. In single-node ``db`` mode the API drives the run in a FastAPI
background task, so the API is usable without a separate worker process.
"""
from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..git import RepoManager
from ..queue import get_queue
from ..storage.models import Repository
from ..telemetry.tracing import Tracer

app = FastAPI(title="AI Agent Harness", version="0.1.0")


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
