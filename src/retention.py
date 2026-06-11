"""Retention policy: bounded lifetimes for workspaces, traces, logs and runs.

Nothing the harness produces lives forever:

* **Workspaces** (cloned repos + edits) of terminal runs are deleted
  ``HARNESS_WORKSPACE_TTL_HOURS`` after the run last changed (default 72h).
  Non-terminal runs are never touched — a crashed run must stay resumable.
* **Run records** — runs, steps, telemetry spans and event logs — are purged
  ``HARNESS_RUN_RETENTION_DAYS`` after completion (default 30d). Deletion is
  a single ``DELETE FROM runs`` per run; steps/telemetry/events follow via
  ``ON DELETE CASCADE``.
* **Orphan workspace directories** (no matching run row — e.g. the run was
  purged on another node, or a crash left a half-prepared dir) are removed
  once they are an hour old, so a directory created by an in-flight
  ``prepare`` is never raced.

The sweep runs in three places: periodically inside every worker's poll loop
(``HARNESS_SWEEP_INTERVAL_SEC``), on demand via ``python3 -m src.main cleanup
[--dry-run]``, and as a Kubernetes CronJob (``infra/k8s/cleanup-cronjob.yaml``)
in the cluster topology.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .orchestrator.states import RunState
from .storage.models import Repository

_ORPHAN_MIN_AGE_SEC = 3600


@dataclass
class SweepReport:
    workspaces_removed: list[str] = field(default_factory=list)
    runs_purged: list[str] = field(default_factory=list)
    orphans_removed: list[str] = field(default_factory=list)
    bytes_reclaimed: int = 0
    dry_run: bool = False

    def summary(self) -> str:
        mode = "DRY-RUN: would reclaim" if self.dry_run else "reclaimed"
        return (f"{mode} {self.bytes_reclaimed} bytes — "
                f"{len(self.workspaces_removed)} workspace(s), "
                f"{len(self.runs_purged)} run record(s), "
                f"{len(self.orphans_removed)} orphan dir(s)")


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def sweep(repo: Repository | None = None, *, now: datetime | None = None,
          dry_run: bool = False) -> SweepReport:
    """Apply the retention policy once and return what was (or would be) done."""
    repo = repo or Repository()
    now = now or datetime.now(timezone.utc)
    report = SweepReport(dry_run=dry_run)

    ws_cutoff = now - timedelta(hours=settings.workspace_ttl_hours)
    purge_cutoff = now - timedelta(days=settings.run_retention_days)
    terminal = {RunState.COMPLETED.value, RunState.FAILED.value,
                RunState.ROLLED_BACK.value}

    known_run_ids: set[str] = set()
    for run in repo.list_runs():
        known_run_ids.add(run.run_id)
        if run.status not in terminal:
            continue  # never touch a resumable run
        updated = _parse_ts(run.updated_at)

        # 1) expire the workspace
        ws = Path(run.workspace_path).parent if run.workspace_path else None
        if ws and ws.exists() and updated <= ws_cutoff:
            report.bytes_reclaimed += _dir_size(ws)
            report.workspaces_removed.append(str(ws))
            if not dry_run:
                shutil.rmtree(ws, ignore_errors=True)
                run.workspace_path = None
                repo.update_run(run)
                repo.add_event(run.run_id, "INFO",
                               f"workspace removed by retention sweep (> "
                               f"{settings.workspace_ttl_hours}h old)",
                               stage="RETENTION")

        # 2) purge the whole run record (steps/telemetry/events cascade)
        if updated <= purge_cutoff:
            report.runs_purged.append(run.run_id)
            if run.workspace_path:
                ws2 = Path(run.workspace_path).parent
                if ws2.exists() and str(ws2) not in report.workspaces_removed:
                    report.bytes_reclaimed += _dir_size(ws2)
                    if not dry_run:
                        shutil.rmtree(ws2, ignore_errors=True)
            if not dry_run:
                repo.delete_run(run.run_id)

    # 3) orphan workspace directories (no run row owns them)
    root = settings.workspaces_root
    if root.exists():
        for entry in root.iterdir():
            if not entry.is_dir() or entry.name in known_run_ids:
                continue
            try:
                age = time.time() - entry.stat().st_mtime
            except OSError:
                continue
            if age < _ORPHAN_MIN_AGE_SEC:
                continue  # might belong to a run being prepared right now
            report.bytes_reclaimed += _dir_size(entry)
            report.orphans_removed.append(str(entry))
            if not dry_run:
                shutil.rmtree(entry, ignore_errors=True)

    return report
