"""Stage 1 — Planner (READ-ONLY).

Scans the workspace tree, reads a bounded set of key source snippets, builds a
lightweight dependency hint, and asks the LLM for a strict-JSON execution plan.
The Planner NEVER mutates the workspace — it only calls sandbox *read* tools —
and its output is validated into typed :class:`Step` rows before anything runs.
"""
from __future__ import annotations

import re

from ..llm import prompts
from ..sandbox.base import Sandbox
from ..storage.models import Step
from ..telemetry.tracing import Tracer

_SOURCE_EXT = (".py", ".js", ".ts", ".go", ".rb", ".java")
_MAX_SNIPPETS = 8
_SNIPPET_CHARS = 1800


class PlanError(RuntimeError):
    pass


class Planner:
    def __init__(self, llm, sandbox: Sandbox, tracer: Tracer) -> None:
        self.llm = llm
        self.sb = sandbox
        self.tracer = tracer

    def plan(self, run_id: str, task: str) -> tuple[dict, list[Step]]:
        tree = self.sb.list_tree()
        snippets = self._collect_snippets(tree, task)
        user = prompts.embed_context(
            f"TASK:\n{task}\n\nProduce the execution plan for this repository.",
            {"role": "planner", "task": task, "tree": tree,
             "snippets": snippets, "dependency_hints": self._dep_hints(tree)},
        )

        with self.tracer.span("planner") as span:
            resp = self.llm.complete(prompts.PLANNER_SYSTEM, user, max_tokens=4096)
            span.add_tokens(resp.input_tokens, resp.output_tokens)
            span.set_status("ok")

        plan = prompts.extract_json_object(resp.text)
        steps = self._validate(run_id, plan, tree)
        return plan, steps

    # -- helpers -------------------------------------------------------------
    def _collect_snippets(self, tree: list[str], task: str) -> dict[str, str]:
        tokens = {t for t in re.split(r"\W+", task.lower()) if len(t) > 2}
        sources = [p for p in tree if p.endswith(_SOURCE_EXT)
                   and not re.search(r"(^|/)(node_modules|dist|build)/", p)]

        def relevance(p: str) -> int:
            low = p.lower()
            return sum(1 for t in tokens if t in low) + (
                1 if re.search(r"(^|/)(app|src|lib)/", low) else 0)

        ranked = sorted(sources, key=relevance, reverse=True)[:_MAX_SNIPPETS]
        out: dict[str, str] = {}
        for p in ranked:
            try:
                out[p] = self.sb.read_file(p)[:_SNIPPET_CHARS]
            except Exception:
                continue
        return out

    @staticmethod
    def _dep_hints(tree: list[str]) -> dict:
        """Cheap project-shape signal for the planner (no full graph needed)."""
        return {
            "has_package_json": "package.json" in tree,
            "has_pyproject": "pyproject.toml" in tree,
            "has_tests_dir": any(p.startswith(("tests/", "test/")) for p in tree),
            "test_files": [p for p in tree
                           if re.search(r"(test_|_test|\.test\.|\.spec\.)", p)][:20],
        }

    def _validate(self, run_id: str, plan: dict, tree: list[str]) -> list[Step]:
        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanError("plan has no steps")
        tree_set = set(tree)
        default_checks = self._default_checks(tree)
        steps: list[Step] = []
        for i, s in enumerate(raw_steps):
            file = s.get("file")
            action = (s.get("action") or "modify").lower()
            if not file:
                raise PlanError(f"step {i} missing 'file'")
            if action == "modify" and file not in tree_set:
                # A modify target must exist; otherwise treat as create.
                action = "create"
            checks = s.get("checks") or default_checks
            steps.append(Step(
                run_id=run_id, step_index=i, step_id=s.get("id", f"step-{i+1}"),
                file=file, action=action, reason=s.get("reason", ""), checks=checks,
            ))
        return steps

    @staticmethod
    def _default_checks(tree: list[str]) -> list[str]:
        if "package.json" in tree:
            return ["node --test", "node scripts/lint.js"]
        return ["python3 -m unittest discover -s tests -t .", "python3 scripts/lint.py"]
