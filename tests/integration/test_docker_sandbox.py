"""DockerSandbox against a real daemon: isolation, mounts, limits, teardown."""
import pytest

from conftest import PY_REPO  # noqa: F401  (parent conftest sets hermetic env)
from integration.conftest import docker_image

_IMAGE = docker_image()
pytestmark = [
    pytest.mark.integration, pytest.mark.docker,
    pytest.mark.skipif(_IMAGE is None,
                       reason="no Docker daemon or no suitable local image"),
]


@pytest.fixture
def sandbox(tmp_path):
    from src.sandbox.docker_runner import DockerSandbox

    sb = DockerSandbox(str(tmp_path), _IMAGE)
    yield sb
    sb.teardown()


def test_exec_runs_inside_container_with_workspace_mounted(sandbox):
    sandbox.write_file("hello.txt", "from the host\n")
    r = sandbox.exec("cat hello.txt", timeout=30)
    assert r.ok and "from the host" in r.stdout


def test_exit_codes_and_stderr_are_captured(sandbox):
    r = sandbox.exec("echo oops >&2; exit 3", timeout=30)
    assert not r.ok and r.exit_code == 3 and "oops" in r.stderr


def test_network_is_disabled(sandbox):
    # --network none: any TCP connect attempt must fail fast.
    r = sandbox.exec(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), 3)\""
        " 2>/dev/null || echo NO_NETWORK", timeout=30)
    assert "NO_NETWORK" in r.stdout


def test_root_filesystem_is_read_only(sandbox):
    r = sandbox.exec("touch /evil 2>&1 || echo READ_ONLY_OK", timeout=30)
    assert "READ_ONLY_OK" in r.stdout


def test_timeout_is_enforced_in_container(sandbox):
    r = sandbox.exec("sleep 30", timeout=2)
    assert r.timed_out and not r.ok


def test_teardown_removes_the_container(tmp_path):
    import docker as docker_sdk

    from src.sandbox.docker_runner import DockerSandbox

    sb = DockerSandbox(str(tmp_path), _IMAGE)
    sb.exec("true", timeout=30)
    container_id = sb._container.id
    sb.teardown()
    client = docker_sdk.from_env()
    with pytest.raises(docker_sdk.errors.NotFound):
        client.containers.get(container_id)
