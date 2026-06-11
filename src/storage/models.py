"""Domain models + the repository (data-access object) over the state store.

Nothing above this layer touches SQL. The :class:`Repository` exposes the exact
operations the state machine and worker need, each of which commits immediately
(autocommit connection) so a crash never leaves a half-written transition.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import get_connection, init_db
from ..orchestrator.states import RunState, StepStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


# ── Dataclasses ──────────────────────────────────────────────────────────────
@dataclass
class Step:
    run_id: str
    step_index: int
    step_id: str
    file: str
    action: str
    reason: str
    checks: list[str]
    depends_on: list[str] = field(default_factory=list)
    status: str = StepStatus.PENDING.value
    iterations: int = 0
    last_error: dict | None = None
    id: int | None = None

    @classmethod
    def from_row(cls, r) -> "Step":
        return cls(
            id=r["id"], run_id=r["run_id"], step_index=r["step_index"],
            step_id=r["step_id"], file=r["file"], action=r["action"],
            reason=r["reason"], checks=json.loads(r["checks_json"]),
            depends_on=json.loads(r["depends_on_json"]) if r["depends_on_json"] else [],
            status=r["status"], iterations=r["iterations"],
            last_error=json.loads(r["last_error"]) if r["last_error"] else None,
        )


@dataclass
class Run:
    run_id: str
    repo_url: str
    task: str
    status: str = RunState.PENDING.value
    branch: str | None = None
    workspace_path: str | None = None
    base_ref: str | None = None
    current_step: int = 0
    total_steps: int = 0
    plan_json: str | None = None
    tokens_used: int = 0
    error: str | None = None
    pr_url: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def plan(self) -> dict | None:
        return json.loads(self.plan_json) if self.plan_json else None

    @classmethod
    def from_row(cls, r) -> "Run":
        return cls(
            run_id=r["run_id"], repo_url=r["repo_url"], task=r["task"],
            status=r["status"], branch=r["branch"], workspace_path=r["workspace_path"],
            base_ref=r["base_ref"], current_step=r["current_step"],
            total_steps=r["total_steps"], plan_json=r["plan_json"],
            tokens_used=r["tokens_used"], error=r["error"], pr_url=r["pr_url"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        )


# ── Repository ───────────────────────────────────────────────────────────────
class Repository:
    """Thin DAO. One instance per worker/thread; cheap to construct."""

    def __init__(self) -> None:
        init_db()
        self.conn = get_connection()

    # -- runs ----------------------------------------------------------------
    def create_run(self, repo_url: str, task: str, branch: str) -> Run:
        run = Run(run_id=new_run_id(), repo_url=repo_url, task=task, branch=branch)
        self.conn.execute(
            """INSERT INTO runs (run_id, repo_url, task, branch, status,
                                 current_step, total_steps, tokens_used,
                                 created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run.run_id, run.repo_url, run.task, run.branch, run.status,
             0, 0, 0, run.created_at, run.updated_at),
        )
        return run

    def get_run(self, run_id: str) -> Run | None:
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return Run.from_row(r) if r else None

    def list_runs(self, status: str | None = None) -> list[Run]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC"
            ).fetchall()
        return [Run.from_row(r) for r in rows]

    def list_resumable(self) -> list[Run]:
        """Non-terminal runs a restarted worker should pick back up."""
        terminal = (RunState.COMPLETED.value, RunState.FAILED.value, RunState.ROLLED_BACK.value)
        rows = self.conn.execute(
            f"SELECT * FROM runs WHERE status NOT IN ({','.join('?' * len(terminal))}) "
            f"ORDER BY created_at ASC",
            terminal,
        ).fetchall()
        return [Run.from_row(r) for r in rows]

    def claim_pending(self) -> Run | None:
        """Atomically move one PENDING run to PLANNING and return it.

        The conditional UPDATE is the lock: two workers racing for the same row
        cannot both flip it, because only one ``rowcount==1`` wins.
        """
        row = self.conn.execute(
            "SELECT run_id FROM runs WHERE status=? ORDER BY created_at ASC LIMIT 1",
            (RunState.PENDING.value,),
        ).fetchone()
        if not row:
            return None
        cur = self.conn.execute(
            "UPDATE runs SET status=?, updated_at=? WHERE run_id=? AND status=?",
            (RunState.PLANNING.value, _now(), row["run_id"], RunState.PENDING.value),
        )
        if cur.rowcount != 1:
            return None  # lost the race; let caller poll again
        return self.get_run(row["run_id"])

    def update_run(self, run: Run) -> None:
        # NB: tokens_used is intentionally NOT written here. It is owned by
        # add_tokens() (an atomic SQL increment from the tracer); writing the
        # in-memory value back would clobber concurrent increments.
        run.updated_at = _now()
        self.conn.execute(
            """UPDATE runs SET status=?, branch=?, workspace_path=?, base_ref=?,
                   current_step=?, total_steps=?, plan_json=?, error=?, pr_url=?,
                   updated_at=?
               WHERE run_id=?""",
            (run.status, run.branch, run.workspace_path, run.base_ref,
             run.current_step, run.total_steps, run.plan_json,
             run.error, run.pr_url, run.updated_at, run.run_id),
        )

    def add_tokens(self, run_id: str, tokens: int) -> int:
        self.conn.execute(
            "UPDATE runs SET tokens_used = tokens_used + ?, updated_at=? WHERE run_id=?",
            (tokens, _now(), run_id),
        )
        r = self.conn.execute(
            "SELECT tokens_used FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return r["tokens_used"] if r else 0

    # -- steps ---------------------------------------------------------------
    def replace_steps(self, run_id: str, steps: list[Step]) -> None:
        self.conn.execute("DELETE FROM steps WHERE run_id=?", (run_id,))
        for s in steps:
            self.conn.execute(
                """INSERT INTO steps (run_id, step_index, step_id, file, action,
                       reason, checks_json, depends_on_json, status, iterations,
                       last_error, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, s.step_index, s.step_id, s.file, s.action, s.reason,
                 json.dumps(s.checks), json.dumps(s.depends_on), s.status,
                 s.iterations,
                 json.dumps(s.last_error) if s.last_error else None, _now(), _now()),
            )

    def get_steps(self, run_id: str) -> list[Step]:
        rows = self.conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY step_index ASC", (run_id,)
        ).fetchall()
        return [Step.from_row(r) for r in rows]

    def get_step(self, run_id: str, step_index: int) -> Step | None:
        r = self.conn.execute(
            "SELECT * FROM steps WHERE run_id=? AND step_index=?", (run_id, step_index)
        ).fetchone()
        return Step.from_row(r) if r else None

    def update_step(self, step: Step) -> None:
        self.conn.execute(
            """UPDATE steps SET status=?, iterations=?, last_error=?, updated_at=?
               WHERE run_id=? AND step_index=?""",
            (step.status, step.iterations,
             json.dumps(step.last_error) if step.last_error else None, _now(),
             step.run_id, step.step_index),
        )

    # -- telemetry -----------------------------------------------------------
    def add_telemetry(self, run_id: str, agent: str, *, step_id: str | None = None,
                      input_tokens: int = 0, output_tokens: int = 0,
                      duration_ms: int = 0, verification_iteration: int | None = None,
                      status: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO telemetry (run_id, step_id, agent, input_tokens,
                   output_tokens, duration_ms, verification_iteration, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, step_id, agent, input_tokens, output_tokens, duration_ms,
             verification_iteration, status, _now()),
        )

    def get_telemetry(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM telemetry WHERE run_id=? ORDER BY id ASC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- events (run log) ----------------------------------------------------
    def add_event(self, run_id: str, level: str, message: str, *,
                  stage: str | None = None, data: dict | None = None) -> int:
        """Persist a run event, then push it to live subscribers.

        The DB row is the durable record (and the SSE replay source); the bus
        publish is the real-time path. Returns the event id so streaming
        clients can resume from it (SSE ``Last-Event-ID``).
        """
        ts = _now()
        row = self.conn.execute(
            "INSERT INTO events (run_id, ts, level, stage, message, data_json) "
            "VALUES (?,?,?,?,?,?) RETURNING id",
            (run_id, ts, level, stage, message,
             json.dumps(data) if data else None),
        ).fetchone()
        event_id = row["id"] if row else 0
        from ..events import get_bus  # lazy: avoid import cycle at module load

        get_bus().publish(run_id, {
            "id": event_id, "run_id": run_id, "ts": ts, "level": level,
            "stage": stage, "message": message, "data_json":
                json.dumps(data) if data else None,
        })
        return event_id

    def get_events(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY id ASC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_events_since(self, run_id: str, after_id: int) -> list[dict]:
        """Events with id > ``after_id`` — the SSE replay/catch-up query."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id ASC",
            (run_id, after_id),
        ).fetchall()
        return [dict(r) for r in rows]
