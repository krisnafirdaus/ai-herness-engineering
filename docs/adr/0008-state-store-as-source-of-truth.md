# ADR 0008 — One durable state store; everything else is derived or lossy

**Status:** accepted (documents a founding decision the newer ADRs build on)

## Context

A resumable multi-agent system accumulates candidate "sources of truth":
process memory, the work queue, the orchestration framework's checkpointer,
the event bus, the workspace on disk. Crash recovery is only tractable if
exactly one of them wins.

## Decision

The SQL state store (SQLite single-node, Postgres in the cluster; one
dialect-adapted repository layer) is **the** source of truth:

* every state transition is committed (autocommit — no half-written
  transitions) *before* the next phase begins; `drive()` is therefore
  idempotent to restart from any point;
* the **queue** only carries run ids; a lost message is recovered by the
  workers' startup scan for non-terminal runs (`list_resumable`). Redis can
  be flushed without losing work.
* the **event bus** is allowed to be lossy; the events table is the durable
  log and the SSE replay source (ADR 0002).
* the **LangGraph engine** routes on the persisted status and keeps no
  durable state of its own (ADR 0001).
* the **workspace** is reconstructable: re-clone + cached plan + completed
  steps tracked in the store. (Workspaces of *resumable* runs are kept —
  retention only touches terminal runs, ADR 0007.)
* the plan is cached (`runs.plan_json`), so resume never re-pays the
  Planner; per-step `status`/`iterations`/`last_error` make retries
  crash-consistent.

Schema changes ship as **additive in-place migrations** (`ALTER TABLE …`
ignored when the column exists) — runs in flight during an upgrade keep
working; no migration tooling required at this size.

## Consequences

* A worker can be SIGKILLed at any line and `recover()` finishes the run
  with no duplicate Planner calls and no re-verified steps (proven by
  `test_crash_recovery_resumes_without_replanning`, on both engines).
* Multi-writer correctness reduces to one conditional UPDATE
  (`claim_pending`), verified against real Postgres in the integration
  suite.
