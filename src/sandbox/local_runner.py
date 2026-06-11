"""Local isolated-workspace sandbox (the TRUSTED-code fallback).

Each run gets its OWN directory under ``workspaces/runs/{run_id}/repo`` — there
is no shared global volume, so two runs never see each other's files. Commands
execute via subprocess with ``cwd`` pinned to that workspace, hardened with:

* **scrubbed environment** — only an allowlist (``PATH``, ``HOME``, locale,
  tmpdir) crosses into the child. Harness secrets (LLM API keys, GitHub
  tokens, DB URLs) are never visible to repo code;
* **its own process group** + group kill on timeout, so a test that forks
  children cannot leave orphans running after the deadline;
* **resource limits** via a portable ``ulimit`` prelude — no core dumps,
  CPU-time ≈ the step timeout, bounded file size (and optionally process
  count via ``HARNESS_LOCAL_NPROC``).

What it does NOT provide: kernel namespace, filesystem-root, or network
isolation. That is why :func:`src.sandbox.select_sandbox` refuses to fall back
here for repos cloned from remote URLs (untrusted code) unless the operator
explicitly opts in — see ``HARNESS_ALLOW_LOCAL_UNTRUSTED``. Untrusted,
multi-tenant execution belongs in the Docker or Kubernetes backends.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from ..config import settings
from .base import ExecResult, Sandbox

# Environment allowlist: enough to run a typical test/lint toolchain, nothing
# that can leak harness credentials into untrusted repo code.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                  "TERM", "SHELL")


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    # Keep the workspace clean of build artifacts so diffs only show real edits.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class LocalSandbox(Sandbox):
    backend = "local"

    def _limits_prelude(self, timeout: int) -> str:
        """POSIX ulimit prelude: core dumps off, CPU + file-size bounded."""
        parts = [
            "ulimit -c 0",                    # no core dumps
            f"ulimit -t {int(timeout) + 30}",  # CPU seconds (wall guard is ours)
            "ulimit -f 1048576",              # max file size: 512 MiB
        ]
        if settings.local_sandbox_nproc > 0:
            parts.append(f"ulimit -u {settings.local_sandbox_nproc}")
        # Limit failures (e.g. an already-lower hard limit) must not break the
        # command itself.
        return "{ " + "; ".join(parts) + "; } 2>/dev/null; "

    def exec(self, command: str, timeout: int) -> ExecResult:
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            self._limits_prelude(timeout) + command,
            shell=True, cwd=str(self.workspace),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_scrubbed_env(),
            start_new_session=True,  # own process group -> killable as a unit
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            dur = int((time.perf_counter() - t0) * 1000)
            return ExecResult(command, proc.returncode, stdout, stderr, dur)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            stdout, stderr = proc.communicate()
            dur = int((time.perf_counter() - t0) * 1000)
            return ExecResult(
                command, 124, stdout or "",
                (stderr or "") + f"\n[timeout after {timeout}s; process group killed]",
                dur, timed_out=True,
            )

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        """Kill the whole process group so forked children don't outlive us."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
