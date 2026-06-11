"""GitHub integration: push the run branch and open a pull request.

Implementation notes (production posture):

* Pure stdlib (``urllib``) REST client — no SDK dependency for one endpoint.
* The token is passed to ``git push`` via ``GIT_CONFIG_*`` **environment**
  variables (an ``http.extraHeader`` Basic credential), never via argv or the
  stored remote, so it cannot leak through process listings or ``.git/config``.
* Harness clones are shallow (``--depth 1``); GitHub refuses pushes from a
  shallow repository, so the branch is unshallowed on demand before pushing.
* PR creation is idempotent: a 422 "already exists" resolves to the existing
  open PR's URL instead of failing the run.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..config import settings

_GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)


class GitHubError(RuntimeError):
    pass


def parse_github_repo(url: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from an https/ssh GitHub URL, else None."""
    m = _GITHUB_URL_RE.match(url.strip())
    return (m.group("owner"), m.group("repo")) if m else None


@dataclass
class PullRequest:
    url: str
    number: int
    existing: bool = False


class GitHubClient:
    """Minimal GitHub REST v3 client (stdlib only, transport injectable)."""

    def __init__(self, token: str, api_url: str | None = None,
                 transport=None) -> None:
        if not token:
            raise GitHubError(
                "no GitHub token configured (set GITHUB_TOKEN or HARNESS_GITHUB_TOKEN)")
        self.token = token
        self.api_url = (api_url or settings.github_api_url).rstrip("/")
        self._transport = transport or urllib.request.urlopen

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ai-agent-harness",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._transport(req) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise GitHubError(f"GitHub API {method} {path} -> {e.code}: {detail}") from e

    def default_branch(self, owner: str, repo: str) -> str:
        return self._request("GET", f"/repos/{owner}/{repo}").get(
            "default_branch", "main")

    def create_pull_request(self, owner: str, repo: str, *, head: str, base: str,
                            title: str, body: str) -> PullRequest:
        try:
            data = self._request("POST", f"/repos/{owner}/{repo}/pulls", {
                "title": title, "head": head, "base": base, "body": body,
            })
            return PullRequest(url=data["html_url"], number=data["number"])
        except GitHubError as e:
            if "422" not in str(e):
                raise
            # Idempotency: a PR for this head may already be open.
            existing = self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls?head={owner}:{head}&state=open")
            if isinstance(existing, list) and existing:
                return PullRequest(url=existing[0]["html_url"],
                                   number=existing[0]["number"], existing=True)
            raise


# ── pushing the run branch ────────────────────────────────────────────────────
def _push_auth_env(token: str) -> dict[str, str]:
    """Auth for ``git push`` via GIT_CONFIG_* env vars (token never in argv)."""
    cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {cred}",
        # Never fall back to an interactive credential prompt in a worker.
        "GIT_TERMINAL_PROMPT": "0",
    }


def push_branch(repo_dir: str, owner: str, repo: str, branch: str,
                token: str) -> None:
    """Push the run branch to GitHub over https with header-based auth."""
    import os

    repo_path = Path(repo_dir)
    env = {**os.environ, **_push_auth_env(token)}
    url = f"https://github.com/{owner}/{repo}.git"

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo_path), *args],
                              capture_output=True, text=True, env=env)

    # GitHub rejects pushes from shallow clones; unshallow on demand.
    if (repo_path / ".git" / "shallow").exists():
        res = git("fetch", "--unshallow", url)
        if res.returncode != 0:
            raise GitHubError(f"git fetch --unshallow failed: {res.stderr.strip()}")

    res = git("push", url, f"HEAD:refs/heads/{branch}")
    if res.returncode != 0:
        raise GitHubError(f"git push failed: {res.stderr.strip()[:500]}")


# ── run-level orchestration ───────────────────────────────────────────────────
def create_pr_for_run(repository, run, *, client: GitHubClient | None = None) -> str:
    """Push ``run``'s branch and open (or reuse) a PR. Returns the PR URL.

    Raises :class:`GitHubError` with an actionable message when the run is not
    PR-able (not completed, not a GitHub repo, no token). The caller decides
    whether that is fatal — for the auto-PR hook it is logged, never fatal.
    """
    from ..git import RepoManager  # local import to avoid a cycle

    if run.status != "COMPLETED":
        raise GitHubError(f"run {run.run_id} is {run.status}, not COMPLETED")
    if not run.workspace_path:
        raise GitHubError(f"run {run.run_id} has no workspace")
    parsed = parse_github_repo(run.repo_url)
    if not parsed:
        raise GitHubError(
            f"repo {run.repo_url!r} is not a GitHub URL — nothing to open a PR on")
    if run.pr_url:
        return run.pr_url  # idempotent resume

    owner, repo_name = parsed
    token = settings.github_token
    client = client or GitHubClient(token)

    push_branch(run.workspace_path, owner, repo_name, run.branch, token)

    base = client.default_branch(owner, repo_name)
    rm = RepoManager(run.workspace_path)
    changed = rm.changed_files(run.base_ref)
    pr = client.create_pull_request(
        owner, repo_name,
        head=run.branch, base=base,
        title=f"harness: {run.task}"[:256],
        body=_pr_body(run, changed),
    )

    run.pr_url = pr.url
    repository.update_run(run)
    repository.add_event(
        run.run_id, "INFO",
        f"pull request {'found' if pr.existing else 'opened'}: {pr.url}",
        stage="COMPLETED", data={"pr_url": pr.url, "pr_number": pr.number})
    return pr.url


def _pr_body(run, changed_files: list[str]) -> str:
    plan = run.plan or {}
    steps = plan.get("steps", [])
    lines = [
        f"Automated change produced by the agent harness (run `{run.run_id}`).",
        "",
        f"**Task:** {run.task}",
        "",
        "**Plan executed:**",
    ]
    for s in steps:
        lines.append(f"- `{s.get('file')}` — {s.get('reason', '').strip() or s.get('action')}")
    lines += [
        "",
        "**Changed files:** " + (", ".join(f"`{f}`" for f in changed_files) or "(none)"),
        "",
        "Every step was verified green (tests + lint) inside an isolated "
        "sandbox before this PR was opened.",
    ]
    return "\n".join(lines)
