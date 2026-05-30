"""CLI entrypoint for the agent harness.

Examples::

    python -m src.main run --repo ./dummy-repos/python-api-sample \
        --task "Add request validation to the user creation endpoint"

    python -m src.main resume  --run-id run_abc123      # continue after a crash
    python -m src.main recover                          # resume ALL stuck runs
    python -m src.main status  --run-id run_abc123
    python -m src.main traces  --run-id run_abc123      # token/latency breakdown
    python -m src.main log     --run-id run_abc123
    python -m src.main diff    --run-id run_abc123
    python -m src.main list
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import settings
from .git import RepoManager
from .orchestrator.state_machine import StateMachine
from .orchestrator.states import RunState
from .storage.models import Repository
from .telemetry.tracing import Tracer


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


_STATE_COLOR = {
    "COMPLETED": "32", "ROLLED_BACK": "33", "FAILED": "31",
}


def _print_run(repo: Repository, run) -> None:
    color = _STATE_COLOR.get(run.status, "36")
    print(f"\n  run_id      : {run.run_id}")
    print(f"  task        : {run.task}")
    print(f"  status      : {_c(run.status, color)}")
    print(f"  branch      : {run.branch}")
    print(f"  steps       : {run.current_step + (1 if run.total_steps else 0)}/{run.total_steps}")
    print(f"  tokens used : {run.tokens_used} (budget {settings.max_tokens_per_run})")
    if run.error:
        print(f"  error       : {_c(run.error, '31')}")
    steps = repo.get_steps(run.run_id)
    if steps:
        print("  plan/steps  :")
        for s in steps:
            mark = {"COMPLETED": "✓", "FAILED": "✗"}.get(s.status, "•")
            print(f"     {mark} [{s.status:<9}] {s.step_id}: {s.action} {s.file} "
                  f"(iters={s.iterations})")


def cmd_run(args) -> int:
    repo = Repository()
    sm = StateMachine(repo)
    run = repo.create_run(args.repo, args.task, args.branch or "harness/auto")
    print(_c(f"\n▶ starting {run.run_id}", "1"))
    print(f"  provider={settings.llm_provider}  sandbox={settings.sandbox}")
    run = sm.drive(run.run_id)
    _print_run(repo, run)
    if run.status == RunState.COMPLETED.value and run.workspace_path:
        files = RepoManager(run.workspace_path).changed_files(run.base_ref)
        print(f"\n  changed files: {', '.join(files) or '(none)'}")
        print(f"  inspect diff : python -m src.main diff --run-id {run.run_id}")
    print(f"  traces       : python -m src.main traces --run-id {run.run_id}\n")
    return 0 if run.status == RunState.COMPLETED.value else 1


def cmd_resume(args) -> int:
    repo = Repository()
    sm = StateMachine(repo)
    run = sm.drive(args.run_id)
    _print_run(repo, run)
    return 0 if run.status == RunState.COMPLETED.value else 1


def cmd_recover(args) -> int:
    repo = Repository()
    sm = StateMachine(repo)
    stuck = repo.list_resumable()
    if not stuck:
        print("no resumable runs.")
        return 0
    print(f"resuming {len(stuck)} run(s): {', '.join(r.run_id for r in stuck)}")
    for r in stuck:
        run = sm.drive(r.run_id)
        _print_run(repo, run)
    return 0


def cmd_status(args) -> int:
    repo = Repository()
    run = repo.get_run(args.run_id)
    if not run:
        print(f"unknown run: {args.run_id}")
        return 1
    _print_run(repo, run)
    return 0


def cmd_traces(args) -> int:
    repo = Repository()
    rows = repo.get_telemetry(args.run_id)
    summary = Tracer.summarize(rows)
    print(f"\n  telemetry for {args.run_id}")
    print(f"  total tokens : {summary['total_tokens']} "
          f"(in {summary['total_input_tokens']} / out {summary['total_output_tokens']})")
    print(f"  total time   : {summary['total_duration_ms']} ms")
    print("  by agent     :")
    for agent, a in summary["by_agent"].items():
        print(f"     {agent:<9} calls={a['calls']:<3} "
              f"tokens={a['input_tokens'] + a['output_tokens']:<7} {a['duration_ms']} ms")
    print("  spans        :")
    for r in rows:
        it = "" if r["verification_iteration"] is None else f" iter={r['verification_iteration']}"
        print(f"     {r['agent']:<9} {r['step_id'] or '-':<8} "
              f"{r['input_tokens']+r['output_tokens']:>6} tok  "
              f"{r['duration_ms']:>6} ms  {r['status'] or ''}{it}")
    print()
    return 0


def cmd_log(args) -> int:
    repo = Repository()
    for e in repo.get_events(args.run_id):
        lvl = _c(e["level"], {"ERROR": "31", "WARN": "33"}.get(e["level"], "36"))
        print(f"  {e['ts']}  {lvl:<5} [{e['stage'] or '-'}] {e['message']}")
    return 0


def cmd_diff(args) -> int:
    repo = Repository()
    run = repo.get_run(args.run_id)
    if not run or not run.workspace_path:
        print(f"no workspace for {args.run_id}")
        return 1
    print(RepoManager(run.workspace_path).diff(run.base_ref))
    return 0


def cmd_list(args) -> int:
    repo = Repository()
    runs = repo.list_runs(args.status)
    for r in runs:
        color = _STATE_COLOR.get(r.status, "36")
        print(f"  {r.run_id}  {_c(r.status, color):<22}  {r.task[:60]}")
    if not runs:
        print("  (no runs)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.main",
                                description="Autonomous multi-file refactoring agent harness")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="create and drive a new run")
    r.add_argument("--repo", required=True, help="git URL or local path")
    r.add_argument("--task", required=True, help="refactoring task description")
    r.add_argument("--branch", help="target branch name (default harness/auto)")
    r.set_defaults(func=cmd_run)

    rs = sub.add_parser("resume", help="resume a run from its persisted state")
    rs.add_argument("--run-id", required=True)
    rs.set_defaults(func=cmd_resume)

    rc = sub.add_parser("recover", help="resume ALL non-terminal runs (crash recovery)")
    rc.set_defaults(func=cmd_recover)

    st = sub.add_parser("status", help="show run + step state")
    st.add_argument("--run-id", required=True)
    st.set_defaults(func=cmd_status)

    tr = sub.add_parser("traces", help="token/latency telemetry breakdown")
    tr.add_argument("--run-id", required=True)
    tr.set_defaults(func=cmd_traces)

    lg = sub.add_parser("log", help="run event log")
    lg.add_argument("--run-id", required=True)
    lg.set_defaults(func=cmd_log)

    df = sub.add_parser("diff", help="show the run's git diff vs base")
    df.add_argument("--run-id", required=True)
    df.set_defaults(func=cmd_diff)

    ls = sub.add_parser("list", help="list runs")
    ls.add_argument("--status", help="filter by status")
    ls.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
