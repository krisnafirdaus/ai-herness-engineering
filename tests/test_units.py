"""Unit tests for safety primitives and helpers."""
import pytest

from src.llm.prompts import extract_json_object
from src.orchestrator.states import ALLOWED_TRANSITIONS, RunState
from src.sandbox.base import PathEscapeError
from src.sandbox.local_runner import LocalSandbox
from src.sandbox.file_tools import FileTools


def test_path_traversal_is_blocked(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    with pytest.raises(PathEscapeError):
        sb.read_file("../../etc/passwd")
    with pytest.raises(PathEscapeError):
        sb.write_file("/etc/evil", "x")


def test_search_replace_refuses_ambiguous_match(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    sb.write_file("a.txt", "x\nx\n")
    tools = FileTools(sb)
    res = tools.search_replace("a.txt", "x", "y")
    assert not res.ok and "ambiguous" in res.message


def test_search_replace_applies_unique_match(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    sb.write_file("a.txt", "hello world")
    tools = FileTools(sb)
    assert tools.search_replace("a.txt", "world", "harness").ok
    assert sb.read_file("a.txt") == "hello harness"


def test_local_exec_captures_exit_and_output(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    ok = sb.exec("echo hi", timeout=10)
    assert ok.ok and "hi" in ok.stdout
    bad = sb.exec("exit 3", timeout=10)
    assert not bad.ok and bad.exit_code == 3


def test_exec_timeout(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    r = sb.exec("sleep 5", timeout=1)
    assert r.timed_out and not r.ok


@pytest.mark.parametrize("text", [
    '{"a": 1}',
    'prose before {"a": 1} prose after',
    '```json\n{"a": 1}\n```',
    'noise {"nested": {"a": 1}} tail',
])
def test_json_extraction_is_robust(text):
    assert extract_json_object(text)  # parses without raising


def test_illegal_transition_not_allowed():
    # PENDING cannot jump straight to COMPLETED.
    assert RunState.COMPLETED not in ALLOWED_TRANSITIONS[RunState.PENDING]
    # FAILED must go through ROLLED_BACK.
    assert ALLOWED_TRANSITIONS[RunState.FAILED] == {RunState.ROLLED_BACK}
