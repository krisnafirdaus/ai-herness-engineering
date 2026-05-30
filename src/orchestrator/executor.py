"""Stage 2 — Executor (SANDBOXED, single step).

Applies exactly one plan step by asking the LLM for the complete new file
content, then writing it through the traversal-guarded :class:`FileTools`. On a
retry it is handed the previous verification failure so it can diagnose and fix
rather than blindly re-apply the same edit.
"""
from __future__ import annotations

from ..llm import prompts
from ..sandbox.file_tools import FileTools
from ..storage.models import Step
from ..telemetry.tracing import Tracer


class ExecError(RuntimeError):
    pass


class Executor:
    def __init__(self, llm, tools: FileTools, tracer: Tracer) -> None:
        self.llm = llm
        self.tools = tools
        self.tracer = tracer

    def execute(self, run_id: str, task: str, step: Step,
                last_error: dict | None) -> str:
        current = ""
        if step.action != "create":
            r = self.tools.read_file(step.file)
            current = r.content or ""

        verb = "Fix the previous failed attempt" if last_error else "Apply this step"
        user = prompts.embed_context(
            f"TASK:\n{task}\n\n{verb} for file `{step.file}`.\n"
            f"Reason: {step.reason}",
            {"role": "executor", "task": task,
             "step": {"file": step.file, "action": step.action, "reason": step.reason},
             "current_content": current, "last_error": last_error},
        )

        with self.tracer.span("executor", step_id=step.step_id,
                              iteration=step.iterations) as span:
            resp = self.llm.complete(prompts.EXECUTOR_SYSTEM, user, max_tokens=8192)
            span.add_tokens(resp.input_tokens, resp.output_tokens)
            span.set_status("retry" if last_error else "apply")

        edit = prompts.extract_json_object(resp.text)
        return self._apply(step, edit)

    def _apply(self, step: Step, edit: dict) -> str:
        file = edit.get("file") or step.file
        action = (edit.get("action") or step.action).lower()

        if action == "delete":
            res = self.tools.delete_file(file)
        else:
            content = edit.get("content")
            if content is None:
                raise ExecError(f"executor returned no content for {file}")
            res = self.tools.write_file(file, content)

        if not res.ok:
            raise ExecError(res.message)
        return edit.get("summary") or res.message
