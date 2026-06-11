# ADR 0007 — Retention and cleanup policy

**Status:** accepted

## Context

Workspaces (full clones + edits), telemetry spans, event logs and run rows
accumulated forever. At production run volume that is disk exhaustion on
workers and unbounded DB growth — and stale clones of third-party code are
also a liability.

## Decision

One sweep (`src/retention.py`) applies three rules:

1. **Workspace TTL** — terminal runs (`COMPLETED`/`FAILED`/`ROLLED_BACK`)
   lose their workspace `HARNESS_WORKSPACE_TTL_HOURS` (72h default) after
   their last update. The run record stays (audit trail); `workspace_path`
   is nulled and an event is logged. **Non-terminal runs are never touched**
   — a crashed run must stay resumable, so resumability wins over disk.
2. **Record retention** — `HARNESS_RUN_RETENTION_DAYS` (30d default) after
   the last update, the run row is deleted; steps, telemetry and events
   follow via `ON DELETE CASCADE` (one delete, no partial purges).
3. **Orphan reaping** — workspace directories with no owning run row (purged
   elsewhere, or a crash between mkdir and the first commit) are removed once
   they are an hour old; the grace period avoids racing `prepare()`.

It runs in three places: inside every worker's poll loop
(`HARNESS_SWEEP_INTERVAL_SEC`), on demand via `python3 -m src.main cleanup
[--dry-run]`, and as a daily k8s CronJob — so cleanup happens with a worker
pool, without one, and in the cluster topology. `--dry-run` prints the full
report (including bytes) without deleting.

## Consequences

* Disk and DB usage are bounded and tunable per deployment.
* A sweep failure is logged and never kills a worker; deletion order
  (workspace before record) means a crash mid-sweep leaves only re-sweepable
  state.
