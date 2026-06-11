"""Stage 1 — Planner (READ-ONLY).

Scans the workspace tree, builds a **static import-dependency graph** (AST for
Python, import/require resolution for JS/TS), reads a bounded set of key source
snippets, and asks the LLM for a strict-JSON execution plan. The Planner NEVER
mutates the workspace — it only calls sandbox *read* tools — and its output is
validated into typed :class:`Step` rows before anything runs.

Dependency handling is structural, not name-based guessing:

* every candidate file's imports and *dependents* (reverse edges — the blast
  radius of editing it) are embedded in the prompt context;
* each plan step may declare ``depends_on`` (files it builds on), which is
  validated against the plan;
* the final step order is **topologically sorted** so dependencies are edited
  and verified before their dependents, regardless of the order the LLM chose.
  Declared ``depends_on`` edges and discovered import edges are both honored;
  cycles degrade gracefully to the LLM's order rather than failing the plan.
"""
from __future__ import annotations

import re

from ..analysis import DepGraph, build_dep_graph
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
        graph = build_dep_graph(tree, self.sb.read_file)
        snippets = self._collect_snippets(tree, task, graph)
        user = prompts.embed_context(
            f"TASK:\n{task}\n\nProduce the execution plan for this repository.",
            {"role": "planner", "task": task, "tree": tree,
             "snippets": snippets,
             "dependency_graph": graph.summary_for(list(snippets)),
             "project_shape": self._project_shape(tree)},
        )

        with self.tracer.span("planner") as span:
            resp = self.llm.complete(prompts.PLANNER_SYSTEM, user, max_tokens=4096)
            span.add_tokens(resp.input_tokens, resp.output_tokens)
            span.set_status("ok")

        plan = prompts.extract_json_object(resp.text)
        steps = self._validate(run_id, plan, tree)
        steps = self._order_by_dependencies(steps, graph)
        return plan, steps

    # -- helpers -------------------------------------------------------------
    def _collect_snippets(self, tree: list[str], task: str,
                          graph: DepGraph) -> dict[str, str]:
        tokens = {t for t in re.split(r"\W+", task.lower()) if len(t) > 2}
        sources = [p for p in tree if p.endswith(_SOURCE_EXT)
                   and not re.search(r"(^|/)(node_modules|dist|build)/", p)]

        def relevance(p: str) -> tuple:
            low = p.lower()
            name_hits = sum(1 for t in tokens if t in low)
            # Files with many dependents are structurally central — surfacing
            # them helps the planner see what an edit can break.
            centrality = min(len(graph.dependents_of(p)), 5)
            in_src = 1 if re.search(r"(^|/)(app|src|lib)/", low) else 0
            return (name_hits, centrality, in_src)

        ranked = sorted(sources, key=relevance, reverse=True)[:_MAX_SNIPPETS]
        out: dict[str, str] = {}
        for p in ranked:
            try:
                out[p] = self.sb.read_file(p)[:_SNIPPET_CHARS]
            except Exception:
                continue
        return out

    @staticmethod
    def _project_shape(tree: list[str]) -> dict:
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
        plan_files = {s.get("file") for s in raw_steps if isinstance(s, dict)}
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
            declared = s.get("depends_on") or []
            # Only intra-plan dependencies are meaningful for ordering.
            depends_on = [d for d in declared
                          if isinstance(d, str) and d in plan_files and d != file]
            steps.append(Step(
                run_id=run_id, step_index=i, step_id=s.get("id", f"step-{i+1}"),
                file=file, action=action, reason=s.get("reason", ""), checks=checks,
                depends_on=depends_on,
            ))
        return steps

    @staticmethod
    def _order_by_dependencies(steps: list[Step], graph: DepGraph) -> list[Step]:
        """Topologically order steps: dependencies before dependents.

        Edges come from BOTH the plan's declared ``depends_on`` and the static
        import graph. The sort is stable (LLM order breaks ties) and cycles
        fall back to the LLM order instead of failing.
        """
        by_file = {s.file: s for s in steps}
        files = [s.file for s in steps]
        # Merge declared edges into a copy of the import edges.
        merged = DepGraph(
            imports={f: set(graph.imports_of(f)) for f in files},
            dependents={},
        )
        for s in steps:
            merged.imports.setdefault(s.file, set()).update(s.depends_on)
        ordered_files = merged.topo_order(files)
        ordered = [by_file[f] for f in ordered_files]
        for idx, s in enumerate(ordered):
            s.step_index = idx
        return ordered

    @staticmethod
    def _default_checks(tree: list[str]) -> list[str]:
        if "package.json" in tree:
            return ["node --test", "node scripts/lint.js"]
        return ["python3 -m unittest discover -s tests -t .", "python3 scripts/lint.py"]
