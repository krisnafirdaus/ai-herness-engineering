"""Sandbox contract + shared workspace file operations.

File operations are shared by both backends and always act on the run's
workspace directory on the host. They are hardened against path traversal: every
path is resolved and asserted to stay within the workspace root, so a malicious
plan/edit cannot read or clobber ``/etc/passwd`` or the harness source tree.

Command execution (``exec``) is the part that runs untrusted repo code, and is
where the backends differ (subprocess vs. isolated container).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExecResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def tail(self, n: int = 4000) -> str:
        """Combined output, tail-truncated — enough signal for the Executor to
        diagnose, bounded so we never blow the token budget on a stack trace."""
        blob = (self.stdout or "") + ("\n" + self.stderr if self.stderr else "")
        return blob[-n:] if len(blob) > n else blob


class PathEscapeError(Exception):
    """Raised when a file op resolves outside the sandbox workspace."""


class Sandbox:
    """Abstract sandbox. Subclasses implement :meth:`exec` and :meth:`teardown`."""

    def __init__(self, workspace: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    # -- safe path resolution ------------------------------------------------
    def _resolve(self, rel_path: str) -> Path:
        candidate = (self.workspace / rel_path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise PathEscapeError(
                f"path {rel_path!r} escapes sandbox workspace {self.workspace}"
            )
        return candidate

    # -- file tools (shared) -------------------------------------------------
    def read_file(self, rel_path: str) -> str:
        return self._resolve(rel_path).read_text(encoding="utf-8")

    def write_file(self, rel_path: str, content: str) -> None:
        p = self._resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def delete_file(self, rel_path: str) -> None:
        p = self._resolve(rel_path)
        if p.exists():
            p.unlink()

    def exists(self, rel_path: str) -> bool:
        try:
            return self._resolve(rel_path).exists()
        except PathEscapeError:
            return False

    def list_tree(self, max_entries: int = 4000) -> list[str]:
        """Workspace-relative file list, skipping VCS/dependency noise."""
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
        out: list[str] = []
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), self.workspace)
                out.append(rel)
                if len(out) >= max_entries:
                    return sorted(out)
        return sorted(out)

    # -- execution (backend-specific) ----------------------------------------
    def exec(self, command: str, timeout: int) -> ExecResult:  # pragma: no cover
        raise NotImplementedError

    def teardown(self) -> None:  # pragma: no cover
        """Release backend resources (containers). Workspace files are kept so a
        crashed run can be resumed/inspected; the run lifecycle owns cleanup."""
