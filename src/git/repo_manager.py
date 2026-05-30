"""Prepare a per-run git workspace and provide rollback/diff primitives.

Supports two repo sources transparently:
* a remote URL  -> ``git clone --depth 1``
* a local path  -> copied into the workspace and ``git init``'d, so local sample
  repos (which live *inside* this harness repo and aren't standalone clones)
  still get a clean base commit to diff against and roll back to.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Commit identity used for the synthetic base commit / final commit. Set via
# `-c` flags so the harness never depends on the host's global git config.
_GIT_ID = ["-c", "user.name=harness", "-c", "user.email=harness@local"]

_IGNORE = shutil.ignore_patterns(
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "*.pyc"
)


class RepoManager:
    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = Path(repo_dir).resolve()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo_dir), *args],
            capture_output=True, text=True, check=check,
        )

    @staticmethod
    def _is_url(src: str) -> bool:
        return src.startswith(("http://", "https://", "git@", "ssh://"))

    def prepare(self, repo_url: str, branch: str) -> str:
        """Clone/copy ``repo_url`` into the workspace, create ``branch``, and
        return the captured base ref (commit sha) used for diff + rollback."""
        if self.repo_dir.exists():
            shutil.rmtree(self.repo_dir)
        self.repo_dir.parent.mkdir(parents=True, exist_ok=True)

        if self._is_url(repo_url):
            subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(self.repo_dir)],
                capture_output=True, text=True, check=True,
            )
        else:
            src = Path(repo_url).resolve()
            if not src.exists():
                raise FileNotFoundError(f"local repo path does not exist: {src}")
            shutil.copytree(src, self.repo_dir, ignore=_IGNORE)
            self._git("init", "-q")

        # Ensure there is a base commit to anchor diffs/rollback against.
        self._git("add", "-A")
        # Commit only if there is something to commit (URL clones already have HEAD).
        status = self._git("status", "--porcelain").stdout.strip()
        if status or not self._has_head():
            self._git(*_GIT_ID, "commit", "-q", "-m", "harness: base snapshot",
                      check=False)

        base_ref = self._git("rev-parse", "HEAD").stdout.strip()
        # Work on a dedicated branch so the base ref stays pristine.
        self._git("checkout", "-q", "-B", branch)
        return base_ref

    def _has_head(self) -> bool:
        return self._git("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    # Build artifacts that may appear after running tests/lint but are never
    # part of a meaningful refactor diff.
    _EXCLUDE = [":(exclude)**/__pycache__/**", ":(exclude)**/*.pyc",
                ":(exclude)**/node_modules/**", ":(exclude)**/.pytest_cache/**"]

    def diff(self, base_ref: str) -> str:
        """Full staged diff of all current changes (incl. new files) vs base."""
        self._git("add", "-A")
        return self._git("diff", "--staged", base_ref, "--", ".", *self._EXCLUDE,
                         check=False).stdout

    def changed_files(self, base_ref: str) -> list[str]:
        self._git("add", "-A")
        out = self._git("diff", "--staged", "--name-only", base_ref, "--", ".",
                        *self._EXCLUDE, check=False).stdout
        return [l for l in out.splitlines() if l.strip()]

    def rollback(self, base_ref: str) -> None:
        """Discard ALL edits since ``base_ref`` — hard reset + remove untracked."""
        self._git("reset", "--hard", base_ref, check=False)
        self._git("clean", "-fd", check=False)

    def commit_all(self, message: str) -> str:
        """Commit current changes on the run branch (used to ready a PR)."""
        self._git("add", "-A")
        self._git(*_GIT_ID, "commit", "-q", "-m", message, check=False)
        return self._git("rev-parse", "HEAD").stdout.strip()
