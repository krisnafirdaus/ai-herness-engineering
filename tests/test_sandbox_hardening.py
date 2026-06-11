"""LocalSandbox hardening + the fail-closed trust policy."""
import os
import time

import pytest

from src.sandbox import UntrustedSourceError, select_sandbox
from src.sandbox.local_runner import LocalSandbox


def test_secrets_are_scrubbed_from_sandbox_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    sb = LocalSandbox(str(tmp_path))
    r = sb.exec("printenv ANTHROPIC_API_KEY GITHUB_TOKEN", timeout=10)
    assert not r.ok                      # neither variable is visible
    assert "sk-super-secret" not in r.stdout + r.stderr
    # ...but the basics needed to run a toolchain are present.
    r2 = sb.exec("test -n \"$PATH\" && test -n \"$HOME\"", timeout=10)
    assert r2.ok


def test_timeout_kills_whole_process_group(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    t0 = time.time()
    # The parent spawns a backgrounded child; both must die at the deadline.
    r = sb.exec("sh -c 'sleep 30 & sleep 30'", timeout=1)
    assert r.timed_out and not r.ok
    assert time.time() - t0 < 10         # did not wait for the children
    assert "process group killed" in r.stderr


def test_core_dumps_disabled_and_commands_still_work(tmp_path):
    sb = LocalSandbox(str(tmp_path))
    r = sb.exec("ulimit -c", timeout=10)
    assert r.ok and r.stdout.strip() == "0"


def test_untrusted_repo_refuses_local_sandbox(tmp_path, override_settings):
    override_settings(sandbox="local", allow_local_untrusted=False)
    with pytest.raises(UntrustedSourceError, match="HARNESS_ALLOW_LOCAL_UNTRUSTED"):
        select_sandbox(str(tmp_path), trusted=False)


def test_untrusted_local_fallback_requires_explicit_opt_in(tmp_path,
                                                           override_settings):
    override_settings(sandbox="local", allow_local_untrusted=True)
    sb = select_sandbox(str(tmp_path), trusted=False)
    assert isinstance(sb, LocalSandbox)


def test_trusted_local_path_is_allowed(tmp_path, override_settings):
    override_settings(sandbox="local", allow_local_untrusted=False)
    sb = select_sandbox(str(tmp_path), trusted=True)
    assert isinstance(sb, LocalSandbox)


def test_state_machine_marks_remote_repos_untrusted():
    from src.git.repo_manager import RepoManager

    assert RepoManager.is_remote("https://github.com/a/b")
    assert RepoManager.is_remote("git@github.com:a/b.git")
    assert not RepoManager.is_remote("./dummy-repos/python-api-sample")
    assert not RepoManager.is_remote(os.path.abspath("."))
