"""GitHub PR creation: URL parsing, REST client, idempotency, run wiring."""
import io
import json
import urllib.error

import pytest

from src.git.github import (GitHubClient, GitHubError, PullRequest,
                            _push_auth_env, create_pr_for_run,
                            parse_github_repo)


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/octo/widgets", ("octo", "widgets")),
    ("https://github.com/octo/widgets.git", ("octo", "widgets")),
    ("http://github.com/octo/widgets/", ("octo", "widgets")),
    ("git@github.com:octo/widgets.git", ("octo", "widgets")),
    ("ssh://git@github.com/octo/widgets.git", ("octo", "widgets")),
    ("https://gitlab.com/octo/widgets", None),
    ("./dummy-repos/python-api-sample", None),
])
def test_parse_github_repo(url, expected):
    assert parse_github_repo(url) == expected


class _FakeTransport:
    """Scripted urllib transport: maps 'METHOD path' -> (status, payload)."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, req):
        key = f"{req.get_method()} {req.full_url}"
        self.calls.append(key)
        for fragment, (status, payload) in self.routes.items():
            if fragment in key:
                if status >= 400:
                    raise urllib.error.HTTPError(
                        req.full_url, status, "err", {},
                        io.BytesIO(json.dumps(payload).encode()))
                body = io.BytesIO(json.dumps(payload).encode())
                body.__enter__ = lambda *a: body
                body.__exit__ = lambda *a: None
                return body
        raise AssertionError(f"unrouted request: {key}")


def test_create_pull_request_happy_path():
    t = _FakeTransport({
        "POST https://api.github.com/repos/octo/widgets/pulls":
            (201, {"html_url": "https://github.com/octo/widgets/pull/7",
                   "number": 7}),
    })
    pr = GitHubClient("tok", transport=t).create_pull_request(
        "octo", "widgets", head="harness/auto", base="main", title="t", body="b")
    assert pr == PullRequest("https://github.com/octo/widgets/pull/7", 7)


def test_create_pull_request_is_idempotent_on_422():
    t = _FakeTransport({
        "POST https://api.github.com/repos/octo/widgets/pulls":
            (422, {"message": "A pull request already exists"}),
        "GET https://api.github.com/repos/octo/widgets/pulls?head=octo:harness/auto":
            (200, [{"html_url": "https://github.com/octo/widgets/pull/3",
                    "number": 3}]),
    })
    pr = GitHubClient("tok", transport=t).create_pull_request(
        "octo", "widgets", head="harness/auto", base="main", title="t", body="b")
    assert pr.existing and pr.number == 3


def test_missing_token_is_an_actionable_error():
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        GitHubClient("")


def test_push_auth_env_keeps_token_out_of_argv():
    env = _push_auth_env("sekret")
    assert "sekret" not in json.dumps(list(env.keys()))
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_create_pr_for_run_pushes_then_opens_pr(monkeypatch, tmp_path):
    from src.storage.models import Repository

    repo = Repository()
    run = repo.create_run("https://github.com/octo/widgets", "task", "harness/auto")
    run.status = "COMPLETED"
    run.workspace_path = str(tmp_path)
    run.base_ref = "deadbeef"
    run.plan_json = json.dumps({"steps": [{"file": "a.py", "reason": "r"}]})
    repo.update_run(run)

    pushed = {}
    monkeypatch.setattr("src.git.github.push_branch",
                        lambda *a, **k: pushed.setdefault("args", a))
    monkeypatch.setattr("src.git.github.RepoManager", None, raising=False)
    from src.config import settings
    monkeypatch.setattr(type(settings), "github_token", "tok", raising=False)

    class _RM:
        def __init__(self, path): pass
        def changed_files(self, base): return ["a.py"]

    monkeypatch.setattr("src.git.RepoManager", _RM)

    t = _FakeTransport({
        "GET https://api.github.com/repos/octo/widgets":
            (200, {"default_branch": "develop"}),
        "POST https://api.github.com/repos/octo/widgets/pulls":
            (201, {"html_url": "https://github.com/octo/widgets/pull/9",
                   "number": 9}),
    })
    url = create_pr_for_run(repo, run, client=GitHubClient("tok", transport=t))

    assert url == "https://github.com/octo/widgets/pull/9"
    assert pushed["args"][:4] == (str(tmp_path), "octo", "widgets", "harness/auto")
    assert repo.get_run(run.run_id).pr_url == url
    # Second call is a no-op returning the persisted URL (no new API calls).
    calls_before = len(t.calls)
    assert create_pr_for_run(repo, repo.get_run(run.run_id)) == url
    assert len(t.calls) == calls_before


def test_create_pr_for_run_rejects_non_github_repo():
    from src.storage.models import Repository

    repo = Repository()
    run = repo.create_run("./dummy-repos/python-api-sample", "task", "b")
    run.status = "COMPLETED"
    run.workspace_path = "/tmp/x"
    with pytest.raises(GitHubError, match="not a GitHub URL"):
        create_pr_for_run(repo, run)
