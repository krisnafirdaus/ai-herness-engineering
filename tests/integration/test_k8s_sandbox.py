"""K8sSandbox against a real cluster (opt-in: HARNESS_TEST_K8S=1).

Requires the sandbox image to be pullable by the cluster
(HARNESS_SANDBOX_IMAGE; defaults to harness-sandbox:latest).
"""
import pytest

from integration.conftest import k8s_enabled

pytestmark = [
    pytest.mark.integration, pytest.mark.k8s,
    pytest.mark.skipif(not k8s_enabled(),
                       reason="HARNESS_TEST_K8S!=1 or no reachable cluster"),
]


@pytest.fixture
def sandbox(tmp_path):
    from src.config import settings
    from src.sandbox.k8s_runner import K8sSandbox

    sb = K8sSandbox(str(tmp_path / "repo"), settings.sandbox_image)
    yield sb
    sb.teardown()


def test_exec_in_pod_with_synced_workspace(sandbox):
    sandbox.write_file("data.txt", "synced from worker\n")
    r = sandbox.exec("cat data.txt", timeout=60)
    assert r.ok and "synced from worker" in r.stdout


def test_edits_are_resynced_before_next_exec(sandbox):
    sandbox.write_file("v.txt", "one\n")
    assert "one" in sandbox.exec("cat v.txt", timeout=60).stdout
    sandbox.write_file("v.txt", "two\n")
    assert "two" in sandbox.exec("cat v.txt", timeout=60).stdout


def test_exit_codes_and_timeout(sandbox):
    assert sandbox.exec("exit 4", timeout=60).exit_code == 4
    r = sandbox.exec("sleep 30", timeout=2)
    assert r.timed_out


def test_pod_is_deleted_on_teardown(tmp_path):
    from kubernetes import client

    from src.config import settings
    from src.sandbox.k8s_runner import K8sSandbox

    sb = K8sSandbox(str(tmp_path / "repo"), settings.sandbox_image)
    sb.exec("true", timeout=60)
    name, ns = sb.pod_name, sb.namespace
    sb.teardown()

    import time
    core = client.CoreV1Api()
    deadline = time.time() + 30
    gone = False
    while time.time() < deadline and not gone:
        try:
            core.read_namespaced_pod(name, ns)
            time.sleep(1)
        except Exception:
            gone = True
    assert gone
