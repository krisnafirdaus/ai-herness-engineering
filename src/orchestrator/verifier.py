"""Stage 3 — Verifier (SANDBOXED).

Runs each of a step's check commands (tests, linters) inside the sandbox. On the
first failing command it captures stdout/stderr into a structured error-state
blob and returns it so the orchestrator can feed it back to the Executor. The
error blob is the contract between Verifier and Executor.
"""
from __future__ import annotations

from ..config import settings
from ..sandbox.base import Sandbox
from ..storage.models import Step
from ..telemetry.tracing import Tracer


class Verifier:
    def __init__(self, sandbox: Sandbox, tracer: Tracer) -> None:
        self.sb = sandbox
        self.tracer = tracer

    def verify(self, step: Step) -> tuple[bool, dict | None]:
        with self.tracer.span("verifier", step_id=step.step_id,
                              iteration=step.iterations) as span:
            for command in step.checks:
                result = self.sb.exec(command, timeout=settings.step_timeout_sec)
                if not result.ok:
                    span.set_status("failed")
                    return False, self._error_state(step, command, result)
            span.set_status("passed")
            return True, None

    @staticmethod
    def _error_state(step: Step, command: str, result) -> dict:
        return {
            "step_id": step.step_id,
            "iteration": step.iterations,
            "status": "failed",
            "command": command,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": (result.stdout or "")[-2000:],
            "stderr": (result.stderr or "")[-2000:],
            "output_tail": result.tail(2000),
            "next_action": "retry_executor",
        }
