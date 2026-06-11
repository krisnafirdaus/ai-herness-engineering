"""Executor patch protocol: edits apply, bad anchors are recoverable errors."""
import json

import pytest

from src.orchestrator.executor import ExecError, Executor
from src.sandbox.file_tools import FileTools
from src.sandbox.local_runner import LocalSandbox
from src.storage.models import Step
from src.telemetry.tracing import Span


class _NullTracer:
    def span(self, *a, **k):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield Span("executor", None, None)
        return cm()


class _ScriptedLLM:
    """Returns a fixed JSON payload regardless of the prompt."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete(self, system, user, max_tokens=4096):
        from src.llm.client import LLMResponse
        return LLMResponse(json.dumps(self.payload), 10, 10)


def _step(file="a.py", action="modify"):
    return Step(run_id="run_x", step_index=0, step_id="step-1",
                file=file, action=action, reason="test", checks=[])


def _executor(tmp_path, payload, events=None):
    sb = LocalSandbox(str(tmp_path))
    tools = FileTools(sb)
    on_event = (lambda lvl, msg: events.append((lvl, msg))) if events is not None else None
    return sb, Executor(_ScriptedLLM(payload), tools, _NullTracer(), on_event=on_event)


def test_patch_edits_apply_in_order(tmp_path):
    sb, ex = _executor(tmp_path, {
        "file": "a.py", "action": "modify",
        "edits": [
            {"find": "alpha", "replace": "beta"},
            {"find": "beta gamma", "replace": "beta delta"},
        ],
        "summary": "two edits",
    })
    sb.write_file("a.py", "alpha gamma\n")
    assert ex.execute("run_x", "task", _step(), None) == "two edits"
    assert sb.read_file("a.py") == "beta delta\n"


def test_missing_anchor_raises_recoverable_exec_error(tmp_path):
    sb, ex = _executor(tmp_path, {
        "file": "a.py", "action": "modify",
        "edits": [{"find": "no such anchor", "replace": "x"}],
    })
    sb.write_file("a.py", "content\n")
    with pytest.raises(ExecError, match="failed to apply"):
        ex.execute("run_x", "task", _step(), None)
    assert sb.read_file("a.py") == "content\n"  # nothing clobbered


def test_ambiguous_anchor_raises_exec_error(tmp_path):
    sb, ex = _executor(tmp_path, {
        "file": "a.py", "action": "modify",
        "edits": [{"find": "x", "replace": "y"}],
    })
    sb.write_file("a.py", "x\nx\n")
    with pytest.raises(ExecError, match="ambiguous"):
        ex.execute("run_x", "task", _step(), None)


def test_whole_file_rewrite_for_modify_is_fallback_with_warn(tmp_path):
    events = []
    sb, ex = _executor(tmp_path, {
        "file": "a.py", "action": "modify", "content": "rewritten\n",
        "summary": "rewrite",
    }, events=events)
    sb.write_file("a.py", "original\n")
    summary = ex.execute("run_x", "task", _step(), None)
    assert "whole-file fallback" in summary
    assert sb.read_file("a.py") == "rewritten\n"
    assert any(lvl == "WARN" and "whole-file rewrite" in msg for lvl, msg in events)


def test_create_requires_full_content(tmp_path):
    sb, ex = _executor(tmp_path, {
        "file": "new.py", "action": "create", "content": "print('hi')\n",
        "summary": "created",
    })
    assert ex.execute("run_x", "task", _step("new.py", "create"), None) == "created"
    assert sb.read_file("new.py") == "print('hi')\n"


def test_modify_with_neither_edits_nor_content_fails(tmp_path):
    sb, ex = _executor(tmp_path, {"file": "a.py", "action": "modify"})
    sb.write_file("a.py", "content\n")
    with pytest.raises(ExecError, match="neither"):
        ex.execute("run_x", "task", _step(), None)
