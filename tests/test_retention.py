"""Retention sweep: workspace TTL, record purge, orphans, resumable safety."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.orchestrator.states import RunState
from src.retention import sweep
from src.storage.models import Repository


def _make_run(repo, root: Path, status: str, *, age_hours: float):
    run = repo.create_run("./dummy-repos/python-api-sample", "t", "b")
    ws = root / run.run_id / "repo"
    ws.mkdir(parents=True)
    (ws / "file.txt").write_text("x" * 100)
    run.workspace_path = str(ws)
    run.status = status
    repo.update_run(run)
    # Backdate updated_at directly (update_run always stamps "now").
    backdated = (datetime.now(timezone.utc)
                 - timedelta(hours=age_hours)).isoformat(timespec="seconds")
    repo.conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?",
                      (backdated, run.run_id))
    return repo.get_run(run.run_id)


def test_expired_terminal_workspace_is_removed(tmp_path, override_settings):
    override_settings(workspaces_root=tmp_path, workspace_ttl_hours=72,
                      run_retention_days=30)
    repo = Repository()
    old = _make_run(repo, tmp_path, RunState.COMPLETED.value, age_hours=100)
    fresh = _make_run(repo, tmp_path, RunState.COMPLETED.value, age_hours=1)

    report = sweep(repo)

    assert not Path(old.workspace_path).exists()
    assert Path(fresh.workspace_path).exists()
    assert repo.get_run(old.run_id) is not None          # record kept
    assert repo.get_run(old.run_id).workspace_path is None
    assert report.bytes_reclaimed >= 100


def test_resumable_runs_are_never_touched(tmp_path, override_settings):
    override_settings(workspaces_root=tmp_path, workspace_ttl_hours=1,
                      run_retention_days=1)
    repo = Repository()
    stuck = _make_run(repo, tmp_path, RunState.VERIFYING_STEP.value,
                      age_hours=10_000)

    sweep(repo)

    assert Path(stuck.workspace_path).exists()
    assert repo.get_run(stuck.run_id) is not None


def test_old_runs_are_purged_with_cascade(tmp_path, override_settings):
    override_settings(workspaces_root=tmp_path, workspace_ttl_hours=72,
                      run_retention_days=30)
    repo = Repository()
    ancient = _make_run(repo, tmp_path, RunState.ROLLED_BACK.value,
                        age_hours=31 * 24)
    repo.add_telemetry(ancient.run_id, "planner", input_tokens=5)

    report = sweep(repo)

    assert ancient.run_id in report.runs_purged
    assert repo.get_run(ancient.run_id) is None
    assert repo.get_telemetry(ancient.run_id) == []      # cascaded
    assert repo.get_events(ancient.run_id) == []
    assert not Path(ancient.workspace_path).exists()


def test_orphan_workspace_dirs_are_removed(tmp_path, override_settings):
    import os

    override_settings(workspaces_root=tmp_path)
    repo = Repository()
    orphan = tmp_path / "run_doesnotexist"
    orphan.mkdir()
    (orphan / "junk.bin").write_text("zzz")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
    os.utime(orphan, (old, old))

    recent_orphan = tmp_path / "run_alsomissing"
    recent_orphan.mkdir()                                # < 1h old: kept

    report = sweep(repo)

    assert not orphan.exists()
    assert recent_orphan.exists()
    assert str(orphan) in report.orphans_removed


def test_dry_run_reports_without_deleting(tmp_path, override_settings):
    override_settings(workspaces_root=tmp_path, workspace_ttl_hours=72,
                      run_retention_days=30)
    repo = Repository()
    old = _make_run(repo, tmp_path, RunState.COMPLETED.value, age_hours=100)

    report = sweep(repo, dry_run=True)

    assert report.dry_run and report.workspaces_removed
    assert Path(old.workspace_path).exists()             # nothing deleted
