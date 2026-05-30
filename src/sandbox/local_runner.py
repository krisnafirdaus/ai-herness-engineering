"""Local isolated-workspace sandbox (zero-dependency fallback).

Each run gets its OWN directory under ``workspaces/runs/{run_id}/repo`` — there
is no shared global volume, so two runs (or two tenants) never see each other's
files. Commands execute via subprocess with ``cwd`` pinned to that workspace.

This is weaker than the Docker backend: it does NOT provide kernel-level process
or network isolation. It exists so the harness always runs (CI, laptops without
Docker) and is honestly labelled as such in the README's security section.
"""
from __future__ import annotations

import os
import subprocess
import time

from .base import ExecResult, Sandbox

# Keep the workspace clean of build artifacts so diffs only show real edits.
_CLEAN_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


class LocalSandbox(Sandbox):
    backend = "local"

    def exec(self, command: str, timeout: int) -> ExecResult:
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(self.workspace),
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, **_CLEAN_ENV},
            )
            dur = int((time.perf_counter() - t0) * 1000)
            return ExecResult(command, proc.returncode, proc.stdout, proc.stderr, dur)
        except subprocess.TimeoutExpired as e:
            dur = int((time.perf_counter() - t0) * 1000)
            return ExecResult(
                command, 124, e.stdout or "", (e.stderr or "") + f"\n[timeout after {timeout}s]",
                dur, timed_out=True,
            )
