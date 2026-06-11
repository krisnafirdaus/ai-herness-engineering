"""K8s sandbox: pod manifest hardening + workspace tar sync (no cluster needed).

The exec/stream path against a real cluster lives in
tests/integration/test_k8s_sandbox.py.
"""
import io
import tarfile

from src.sandbox.k8s_runner import build_pod_manifest


def _manifest(**kw):
    defaults = dict(name="harness-sb-run-abc123", image="harness-sandbox:latest",
                    namespace="harness", runtime_class=None, run_label="run_x")
    defaults.update(kw)
    return build_pod_manifest(defaults.pop("name"), defaults.pop("image"),
                              **defaults)


def test_pod_manifest_is_hardened():
    m = _manifest()
    spec = m["spec"]
    c = spec["containers"][0]
    sc = c["securityContext"]

    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["restartPolicy"] == "Never"
    assert c["imagePullPolicy"] == "IfNotPresent"  # side-loaded images (kind/airgapped)
    assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 1000
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"] == {"drop": ["ALL"]}
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    assert c["resources"]["limits"]["memory"] == "1Gi"
    # Only emptyDirs are writable; no host mounts, no PVCs.
    assert all("emptyDir" in v for v in spec["volumes"])


def test_pod_manifest_carries_networkpolicy_label():
    m = _manifest()
    assert m["metadata"]["labels"]["app"] == "harness-sandbox"
    assert m["metadata"]["labels"]["harness/run"] == "run_x"
    assert m["metadata"]["namespace"] == "harness"


def test_runtime_class_is_optional_and_propagated():
    assert "runtimeClassName" not in _manifest()["spec"]
    assert _manifest(runtime_class="gvisor")["spec"]["runtimeClassName"] == "gvisor"


def test_workspace_tarball_round_trips(tmp_path, monkeypatch):
    # Build the tarball through the sandbox helper without touching a cluster.
    from src.sandbox.k8s_runner import K8sSandbox

    sb = object.__new__(K8sSandbox)  # skip __init__ (would load kubeconfig)
    sb.workspace = tmp_path
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "users.py").write_text("X = 1\n")
    (tmp_path / "README.md").write_text("hello\n")

    data = K8sSandbox._workspace_tarball(sb)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = {m.name for m in tar.getmembers()}
        assert "./app/users.py" in names
        assert "./README.md" in names
        f = tar.extractfile("./app/users.py")
        assert f.read() == b"X = 1\n"
