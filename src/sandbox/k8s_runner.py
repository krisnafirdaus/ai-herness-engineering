"""Kubernetes sandbox: one hardened, network-isolated pod per run.

This is the multi-tenant production backend the k8s blueprint describes — now
implemented, not just documented. Per run the worker creates a dedicated pod
and ``exec``s each check command into it; the pod is deleted on teardown.

Isolation layers (defense in depth):

* **pod-per-run** — no shared filesystem or process namespace between runs;
  the workspace is an ``emptyDir`` private to the pod, populated by a tar
  stream from the worker (no PVC sharing, no host mounts);
* **securityContext** — ``runAsNonRoot`` (uid 1000), ``allowPrivilegeEscalation:
  false``, all capabilities dropped, read-only root filesystem (only
  ``/workspace`` and ``/tmp`` emptyDirs are writable), RuntimeDefault seccomp;
* **no credentials** — ``automountServiceAccountToken: false`` and
  ``enableServiceLinks: false``: repo code sees no API token, no service env;
* **kernel boundary (optional)** — ``HARNESS_K8S_RUNTIME_CLASS=gvisor`` runs
  the pod under gVisor/Kata (``infra/k8s/runtimeclass-gvisor.yaml``);
* **network deny-all** — pods carry the ``harness-sandbox`` label matched by
  the bundled NetworkPolicy (``infra/k8s/sandbox-networkpolicy.yaml``), which
  blocks all ingress AND egress. The policy is cluster-side by design: the
  sandbox must not be able to opt out of its own network isolation.
* **bounded resources** — CPU/memory/ephemeral-storage limits per pod, plus
  the same in-container ``timeout`` wall-clock guard the Docker backend uses.

File edits still happen on the worker's host-side workspace (the executor's
file tools); the workspace is re-synced into the pod lazily before the next
``exec`` whenever it changed, so checks always run against the latest edit.
"""
from __future__ import annotations

import io
import tarfile
import time
import uuid

from ..config import settings
from .base import ExecResult, Sandbox

_POLL_SEC = 1.0


def build_pod_manifest(name: str, image: str, *, namespace: str,
                       runtime_class: str | None, run_label: str) -> dict:
    """Pure manifest builder (unit-testable without a cluster)."""
    spec: dict = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "terminationGracePeriodSeconds": 5,
        "containers": [{
            "name": "sandbox",
            "image": image,
            "command": ["sh", "-c", "sleep infinity"],
            "workingDir": "/workspace",
            "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
            "resources": {
                "requests": {"cpu": "250m", "memory": "256Mi"},
                "limits": {"cpu": "2", "memory": "1Gi",
                           "ephemeral-storage": "2Gi"},
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "volumeMounts": [
                {"name": "workspace", "mountPath": "/workspace"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }],
        "volumes": [
            {"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
        ],
    }
    if runtime_class:
        spec["runtimeClassName"] = runtime_class
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                # Matched by the deny-all NetworkPolicy — do not rename.
                "app": "harness-sandbox",
                "harness/run": run_label,
            },
        },
        "spec": spec,
    }


class K8sSandbox(Sandbox):
    backend = "k8s"

    def __init__(self, workspace: str, image: str) -> None:
        super().__init__(workspace)
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self.core = client.CoreV1Api()
        self.image = image
        self.namespace = settings.k8s_namespace
        run_label = self.workspace.parent.name or "adhoc"
        self.pod_name = f"harness-sb-{run_label.replace('_', '-')}-{uuid.uuid4().hex[:6]}"[:63]
        self._run_label = run_label
        self._pod_ready = False
        self._dirty = True  # workspace not yet synced into the pod

    # Host-side file edits invalidate the in-pod copy.
    def write_file(self, rel_path: str, content: str) -> None:
        super().write_file(rel_path, content)
        self._dirty = True

    def delete_file(self, rel_path: str) -> None:
        super().delete_file(rel_path)
        self._dirty = True

    # ── pod lifecycle ─────────────────────────────────────────────────────────
    def _ensure_pod(self) -> None:
        if self._pod_ready:
            return
        manifest = build_pod_manifest(
            self.pod_name, self.image, namespace=self.namespace,
            runtime_class=settings.k8s_runtime_class or None,
            run_label=self._run_label)
        self.core.create_namespaced_pod(self.namespace, manifest)
        deadline = time.time() + settings.k8s_pod_startup_sec
        while time.time() < deadline:
            pod = self.core.read_namespaced_pod(self.pod_name, self.namespace)
            phase = pod.status.phase
            if phase == "Running":
                self._pod_ready = True
                return
            if phase in ("Failed", "Succeeded"):
                raise RuntimeError(
                    f"sandbox pod {self.pod_name} entered {phase} before ready")
            time.sleep(_POLL_SEC)
        raise RuntimeError(
            f"sandbox pod {self.pod_name} not Running after "
            f"{settings.k8s_pod_startup_sec}s")

    def _workspace_tarball(self) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(str(self.workspace), arcname=".")
        return buf.getvalue()

    def _sync_workspace(self) -> None:
        """Stream the host workspace into the pod's /workspace emptyDir."""
        if not self._dirty:
            return
        from kubernetes.stream import stream

        resp = stream(
            self.core.connect_get_namespaced_pod_exec,
            self.pod_name, self.namespace,
            command=["sh", "-c",
                     "find /workspace -mindepth 1 -delete 2>/dev/null; "
                     "tar -xf - -C /workspace"],
            stdin=True, stdout=True, stderr=True, tty=False,
            _preload_content=False)
        data = self._workspace_tarball()
        for i in range(0, len(data), 1 << 20):
            resp.write_stdin(data[i:i + (1 << 20)])
        resp.close()
        self._dirty = False

    # ── command execution ─────────────────────────────────────────────────────
    def exec(self, command: str, timeout: int) -> ExecResult:
        from kubernetes.stream import stream

        self._ensure_pod()
        self._sync_workspace()

        wrapped = f"cd /workspace && timeout {int(timeout)}s sh -c {_shquote(command)}"
        t0 = time.perf_counter()
        resp = stream(
            self.core.connect_get_namespaced_pod_exec,
            self.pod_name, self.namespace,
            command=["sh", "-c", wrapped],
            stdin=False, stdout=True, stderr=True, tty=False,
            _preload_content=False)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                stdout_parts.append(resp.read_stdout())
            if resp.peek_stderr():
                stderr_parts.append(resp.read_stderr())
        resp.close()
        exit_code = resp.returncode if resp.returncode is not None else 1
        dur = int((time.perf_counter() - t0) * 1000)
        return ExecResult(command, exit_code, "".join(stdout_parts),
                          "".join(stderr_parts), dur,
                          timed_out=exit_code == 124)

    def teardown(self) -> None:
        if not self._pod_ready:
            return
        try:
            self.core.delete_namespaced_pod(
                self.pod_name, self.namespace, grace_period_seconds=0)
        except Exception:
            pass  # pod may already be gone; never fail a run on cleanup
        finally:
            self._pod_ready = False


def _shquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
