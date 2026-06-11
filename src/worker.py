"""Worker: consumes the run queue and drives runs to completion.

Crash recovery is a first-class startup step: before consuming new work, the
worker re-scans the state store for any run left in a non-terminal state
(EXECUTING_STEP, VERIFYING_STEP, ...) and resumes it from the persisted
transition — no Planner re-call, no re-verification of completed steps.

Run modes::

    python -m src.worker            # continuous: recover, then poll forever
    python -m src.worker --once     # recover + drain currently-runnable, exit
    python -m src.worker --recover  # recover stuck runs only, then exit
"""
from __future__ import annotations

import argparse
import time

from .orchestrator.state_machine import StateMachine
from .queue import get_queue
from .storage.models import Repository


class Worker:
    def __init__(self) -> None:
        self.repo = Repository()
        self.sm = StateMachine(self.repo)
        self.queue = get_queue()

    def recover(self) -> int:
        """Resume every non-terminal run (called on startup)."""
        stuck = self.repo.list_resumable()
        for run in stuck:
            print(f"[worker] recovering {run.run_id} from {run.status}")
            try:
                self.sm.drive(run.run_id)
            except Exception as exc:  # never let one bad run kill the worker
                print(f"[worker] {run.run_id} errored during recovery: {exc}")
        return len(stuck)

    def drive(self, run_id: str) -> None:
        print(f"[worker] driving {run_id}")
        try:
            run = self.sm.drive(run_id)
            print(f"[worker] {run_id} -> {run.status}")
        except Exception as exc:
            print(f"[worker] {run_id} errored: {exc}")

    def run_once(self) -> None:
        self.recover()
        # Drain any PENDING runs claimable right now.
        while True:
            run_id = self.queue.dequeue(timeout=1)
            if not run_id:
                break
            self.drive(run_id)

    def run_forever(self, poll_interval: float = 2.0) -> None:
        from .config import settings

        self.recover()
        print("[worker] ready; polling for runs")
        last_sweep = 0.0
        while True:
            # Periodic retention sweep: workspaces/records don't grow forever.
            if time.time() - last_sweep >= settings.sweep_interval_sec:
                last_sweep = time.time()
                try:
                    from .retention import sweep

                    report = sweep(self.repo)
                    if report.workspaces_removed or report.runs_purged \
                            or report.orphans_removed:
                        print(f"[worker] retention: {report.summary()}")
                except Exception as exc:  # cleanup must never kill the worker
                    print(f"[worker] retention sweep errored: {exc}")

            run_id = self.queue.dequeue(timeout=5)
            if run_id:
                self.drive(run_id)
            else:
                time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.worker")
    p.add_argument("--once", action="store_true", help="recover + drain, then exit")
    p.add_argument("--recover", action="store_true", help="recover stuck runs only")
    args = p.parse_args(argv)

    worker = Worker()
    if args.recover:
        n = worker.recover()
        print(f"[worker] recovered {n} run(s)")
    elif args.once:
        worker.run_once()
    else:
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
