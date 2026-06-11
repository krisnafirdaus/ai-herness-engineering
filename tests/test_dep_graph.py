"""Dependency-graph analysis + planner step ordering."""
import json

from src.analysis import build_dep_graph
from src.orchestrator.planner import Planner
from src.sandbox.local_runner import LocalSandbox
from src.telemetry.tracing import Span


def _graph(files: dict[str, str]):
    return build_dep_graph(sorted(files), lambda p: files[p])


def test_python_absolute_and_relative_imports_resolve():
    files = {
        "app/__init__.py": "",
        "app/db.py": "X = 1\n",
        "app/api.py": "from app import db\nimport app.models\n",
        "app/models.py": "from . import db\n",
        "app/sub/__init__.py": "",
        "app/sub/deep.py": "from ..db import X\n",
    }
    g = _graph(files)
    assert g.imports_of("app/api.py") == {"app/db.py", "app/models.py",
                                          "app/__init__.py"}
    assert "app/db.py" in g.imports_of("app/models.py")
    assert "app/db.py" in g.imports_of("app/sub/deep.py")
    # Reverse edges: editing db.py impacts everything importing it.
    assert {"app/api.py", "app/models.py",
            "app/sub/deep.py"} <= g.dependents_of("app/db.py")


def test_js_require_and_import_resolve():
    files = {
        "src/users.js": "const v = require('./validate');\n",
        "src/validate.js": "module.exports = {};\n",
        "src/app.ts": "import { x } from './lib/util';\nimport fs from 'fs';\n",
        "src/lib/util.ts": "export const x = 1;\n",
    }
    g = _graph(files)
    assert g.imports_of("src/users.js") == {"src/validate.js"}
    assert g.imports_of("src/app.ts") == {"src/lib/util.ts"}  # 'fs' is external


def test_topo_order_puts_dependencies_first():
    files = {
        "a.py": "import b\n",
        "b.py": "import c\n",
        "c.py": "X = 1\n",
    }
    g = _graph(files)
    assert g.topo_order(["a.py", "b.py", "c.py"]) == ["c.py", "b.py", "a.py"]


def test_cycles_are_detected_and_topo_does_not_hang():
    files = {"a.py": "import b\n", "b.py": "import a\n"}
    g = _graph(files)
    assert g.cycles()
    order = g.topo_order(["b.py", "a.py"])
    assert sorted(order) == ["a.py", "b.py"]  # input order preserved, no crash


class _NullTracer:
    def span(self, *a, **k):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield Span("planner", None, None)
        return cm()


class _PlanLLM:
    """Emits a plan whose steps are deliberately dependent-first."""

    def __init__(self, plan: dict) -> None:
        self.plan = plan

    def complete(self, system, user, max_tokens=4096):
        from src.llm.client import LLMResponse
        return LLMResponse(json.dumps(self.plan), 10, 10)


def test_planner_reorders_steps_dependencies_first(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    sb.write_file("app/api.py", "from app import core\n")
    sb.write_file("app/core.py", "X = 1\n")
    sb.write_file("app/__init__.py", "")

    # LLM emits the DEPENDENT first; harness must flip the order.
    plan = {"task": "t", "steps": [
        {"id": "s-api", "file": "app/api.py", "action": "modify",
         "reason": "", "checks": ["true"]},
        {"id": "s-core", "file": "app/core.py", "action": "modify",
         "reason": "", "checks": ["true"]},
    ]}
    _, steps = Planner(_PlanLLM(plan), sb, _NullTracer()).plan("run_x", "task")
    assert [s.file for s in steps] == ["app/core.py", "app/api.py"]
    assert [s.step_index for s in steps] == [0, 1]


def test_planner_honors_declared_depends_on(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    sb.write_file("one.txt", "")
    sb.write_file("two.txt", "")

    # No import edge exists between text files; declared depends_on must win.
    plan = {"task": "t", "steps": [
        {"id": "s1", "file": "one.txt", "action": "modify", "reason": "",
         "checks": ["true"], "depends_on": ["two.txt"]},
        {"id": "s2", "file": "two.txt", "action": "modify", "reason": "",
         "checks": ["true"]},
    ]}
    _, steps = Planner(_PlanLLM(plan), sb, _NullTracer()).plan("run_x", "task")
    assert [s.file for s in steps] == ["two.txt", "one.txt"]
    assert steps[1].depends_on == ["two.txt"]
