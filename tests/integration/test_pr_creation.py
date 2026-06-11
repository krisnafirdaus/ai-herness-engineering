"""Real GitHub PR creation (opt-in: writes to a throwaway repo you own).

Gate:
    HARNESS_TEST_GITHUB_REPO=https://github.com/<you>/<scratch-repo>
    GITHUB_TOKEN=<token with repo scope on that repo>

The test pushes a uniquely-named branch and opens a PR, then closes it via
the API. Use a scratch repository.
"""
import os
import time
import uuid

import pytest

from integration.conftest import network_available

_REPO = os.environ.get("HARNESS_TEST_GITHUB_REPO", "")
_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("HARNESS_GITHUB_TOKEN", "")

pytestmark = [
    pytest.mark.integration, pytest.mark.github_write,
    pytest.mark.skipif(not (_REPO and _TOKEN and network_available()),
                       reason="set HARNESS_TEST_GITHUB_REPO + GITHUB_TOKEN"),
]


def test_push_and_open_pr_against_scratch_repo(tmp_path, override_settings):
    from src.git import RepoManager
    from src.git.github import GitHubClient, create_pr_for_run, parse_github_repo
    from src.storage.models import Repository

    override_settings(github_token=_TOKEN)
    owner, name = parse_github_repo(_REPO)
    branch = f"harness/integration-{uuid.uuid4().hex[:8]}"

    repo = Repository()
    run = repo.create_run(_REPO, "integration: PR creation test", branch)
    rm = RepoManager(str(tmp_path / "repo"))
    run.base_ref = rm.prepare(_REPO, branch)
    run.workspace_path = str(tmp_path / "repo")

    (tmp_path / "repo" / f"harness-{uuid.uuid4().hex[:6]}.txt").write_text(
        f"integration test {time.time()}\n")
    rm.commit_all("harness integration test commit")
    run.status = "COMPLETED"
    repo.update_run(run)

    url = create_pr_for_run(repo, run)
    assert url.startswith(f"https://github.com/{owner}/{name}/pull/")

    # Idempotency: second call returns the same PR without error.
    assert create_pr_for_run(repo, repo.get_run(run.run_id)) == url

    # Cleanup: close the PR.
    client = GitHubClient(_TOKEN)
    number = int(url.rsplit("/", 1)[1])
    client._request("PATCH", f"/repos/{owner}/{name}/pulls/{number}",
                    {"state": "closed"})
