"""Structured file-editing tools exposed to the Executor agent.

These are the ONLY way the Executor mutates the workspace. Each returns a small
result dict (success + message) so a failed edit becomes structured error state
the agent can react to, rather than an exception that aborts the run.

All paths flow through the sandbox's traversal-guarded resolver.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import PathEscapeError, Sandbox


@dataclass
class ToolResult:
    ok: bool
    message: str
    content: str | None = None


class FileTools:
    """Thin, auditable wrapper around the sandbox's file primitives."""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sb = sandbox

    def read_file(self, path: str) -> ToolResult:
        try:
            return ToolResult(True, f"read {path}", self.sb.read_file(path))
        except FileNotFoundError:
            return ToolResult(False, f"file not found: {path}")
        except PathEscapeError as e:
            return ToolResult(False, str(e))

    def write_file(self, path: str, content: str) -> ToolResult:
        try:
            self.sb.write_file(path, content)
            return ToolResult(True, f"wrote {len(content)} bytes to {path}")
        except PathEscapeError as e:
            return ToolResult(False, str(e))

    def delete_file(self, path: str) -> ToolResult:
        try:
            self.sb.delete_file(path)
            return ToolResult(True, f"deleted {path}")
        except PathEscapeError as e:
            return ToolResult(False, str(e))

    def search_replace(self, path: str, find: str, replace: str) -> ToolResult:
        """Replace an exact, UNIQUE occurrence of ``find`` with ``replace``.

        Refusing non-unique matches mirrors a real coding agent's edit tool and
        prevents an ambiguous edit from silently corrupting unrelated code.
        """
        r = self.read_file(path)
        if not r.ok:
            return r
        content = r.content or ""
        count = content.count(find)
        if count == 0:
            return ToolResult(False, f"search string not found in {path}")
        if count > 1:
            return ToolResult(
                False, f"search string is ambiguous in {path} ({count} matches); "
                       f"include more surrounding context to make it unique",
            )
        return self.write_file(path, content.replace(find, replace, 1))
