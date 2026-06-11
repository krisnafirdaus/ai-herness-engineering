"""Cloning a real public GitHub URL through the harness git layer."""
import pytest

from integration.conftest import network_available

pytestmark = [
    pytest.mark.integration, pytest.mark.network,
    pytest.mark.skipif(not network_available(),
                       reason="github.com:443 not reachable"),
]

# Tiny, famously stable public repository.
PUBLIC_REPO = "https://github.com/octocat/Hello-World"


def test_prepare_clones_public_url_and_creates_run_branch(tmp_path):
    from src.git import RepoManager

    rm = RepoManager(str(tmp_path / "repo"))
    base_ref = rm.prepare(PUBLIC_REPO, "harness/integration-test")

    assert len(base_ref) == 40                       # a real commit sha
    assert (tmp_path / "repo" / ".git").exists()
    assert (tmp_path / "repo" / "README").exists()   # Hello-World's only file

    # On the dedicated run branch, not the default branch.
    head = rm._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == "harness/integration-test"


def test_diff_and_rollback_against_real_clone(tmp_path):
    from src.git import RepoManager

    rm = RepoManager(str(tmp_path / "repo"))
    base_ref = rm.prepare(PUBLIC_REPO, "harness/integration-test")

    (tmp_path / "repo" / "README").write_text("harness was here\n")
    assert rm.changed_files(base_ref) == ["README"]
    assert "harness was here" in rm.diff(base_ref)

    rm.rollback(base_ref)
    assert rm.changed_files(base_ref) == []


def test_remote_urls_are_marked_untrusted():
    from src.git.repo_manager import RepoManager

    assert RepoManager.is_remote(PUBLIC_REPO)
