"""Stage 2 — Executor (SANDBOXED, single step, patch-based).

Applies exactly one plan step as a *minimal patch*: an ordered list of exact,
unique search/replace edits applied through the traversal-guarded
:class:`FileTools`. Whole-file content is accepted only for ``create`` (the
file does not exist yet); a whole-file rewrite of an existing file is a
last-resort fallback that is logged as a WARN event so it is visible in the
run log — never the default contract.

On a retry the Executor is handed the previous failure (verification output or
a failed-patch report) so it can diagnose and fix rather than blindly re-apply
the same edit. A patch that fails to apply (stale/ambiguous anchor) raises
:class:`ExecError` with a structured message; the state machine routes that
back into the retry loop instead of aborting the run.
"""
from __future__ import annotations

from typing import Callable

from ..llm import prompts
from ..sandbox.file_tools import FileTools
from ..storage.models import Step
from ..telemetry.tracing import Tracer


class ExecError(RuntimeError):
    """Recoverable executor failure (bad patch, malformed output)."""


class Executor:
    def __init__(self, llm, tools: FileTools, tracer: Tracer,
                 on_event: Callable[[str, str], None] | None = None) -> None:
        self.llm = llm
        self.tools = tools
        self.tracer = tracer
        self._on_event = on_event or (lambda level, msg: None)

    def execute(self, run_id: str, task: str, step: Step,
                last_error: dict | None) -> str:
        current = ""
        if step.action != "create":
            r = self.tools.read_file(step.file)
            current = r.content or ""

        verb = "Fix the previous failed attempt" if last_error else "Apply this step"
        user = prompts.embed_context(
            f"TASK:\n{task}\n\n{verb} for file `{step.file}` as a minimal patch.\n"
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

        try:
            edit = prompts.extract_json_object(resp.text)
        except ValueError as exc:
            raise ExecError(f"executor returned no parseable JSON: {exc}") from exc
        return self._apply(step, edit)

    # -- patch application -----------------------------------------------------
    def _apply(self, step: Step, edit: dict) -> str:
        file = edit.get("file") or step.file
        action = (edit.get("action") or step.action).lower()

        if action == "delete":
            res = self.tools.delete_file(file)
            if not res.ok:
                raise ExecError(res.message)
            return edit.get("summary") or res.message

        if action == "create":
            content = edit.get("content")
            if content is None:
                raise ExecError(f"create step for {file} returned no content")
            res = self.tools.write_file(file, content)
            if not res.ok:
                raise ExecError(res.message)
            return edit.get("summary") or res.message

        # modify — the patch path.
        edits = edit.get("edits")
        if isinstance(edits, list):
            if not edits:
                return edit.get("summary") or f"no-op: no edits for {file}"
            return self._apply_patch(file, edits, edit.get("summary"))

        # Fallback: model ignored the patch contract and sent whole-file
        # content for an existing file. Apply it (progress beats a hard fail)
        # but surface the contract violation in the run log.
        content = edit.get("content")
        if content is not None:
            self._on_event(
                "WARN",
                f"executor fell back to a whole-file rewrite of {file} "
                f"(patch contract violated — no 'edits' list)")
            res = self.tools.write_file(file, content)
            if not res.ok:
                raise ExecError(res.message)
            return (edit.get("summary") or res.message) + " [whole-file fallback]"

        raise ExecError(f"modify step for {file} returned neither 'edits' nor 'content'")

    def _apply_patch(self, file: str, edits: list, summary: str | None) -> str:
        for i, e in enumerate(edits, start=1):
            if not isinstance(e, dict):
                raise ExecError(f"edit {i}/{len(edits)} for {file} is not an object")
            find, replace = e.get("find"), e.get("replace")
            if not isinstance(find, str) or not find or not isinstance(replace, str):
                raise ExecError(
                    f"edit {i}/{len(edits)} for {file} is malformed: "
                    f"'find' must be a non-empty string and 'replace' a string")
            res = self.tools.search_replace(file, find, replace)
            if not res.ok:
                # Structured, actionable failure — becomes last_error for the retry.
                raise ExecError(
                    f"patch edit {i}/{len(edits)} failed to apply on {file}: "
                    f"{res.message}")
        return summary or f"applied {len(edits)} edit(s) to {file}"
