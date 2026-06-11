"""Execution sandbox + file tools.

Two interchangeable backends implement the same :class:`~src.sandbox.base.Sandbox`
contract:

* :class:`~src.sandbox.docker_runner.DockerSandbox` — runs every check command
  inside a per-run container with ``--network none``, a read-only root fs, a
  writable bind-mount limited to that run's workspace, and CPU/memory caps.
* :class:`~src.sandbox.local_runner.LocalSandbox` — a per-run isolated workspace
  directory with subprocess execution; the zero-dependency fallback when no
  Docker daemon is reachable.

``select_sandbox()`` picks between them based on ``HARNESS_SANDBOX``.
"""
from __future__ import annotations

from ..config import settings
from .base import Sandbox
from .local_runner import LocalSandbox


class UntrustedSourceError(RuntimeError):
    """Raised when an untrusted repo would land in a non-isolating sandbox."""


def select_sandbox(workspace: str, image: str | None = None, *,
                   trusted: bool = True) -> Sandbox:
    """Instantiate the configured sandbox for a run's workspace.

    ``auto`` (default) prefers Docker, then Kubernetes (when configured), and
    falls back to the local sandbox only for **trusted** sources. The local
    backend has no kernel/network isolation, so ``trusted=False`` (repos
    cloned from remote URLs) refuses the local fallback unless the operator
    sets ``HARNESS_ALLOW_LOCAL_UNTRUSTED=1`` — failing closed beats silently
    degrading the security boundary.
    """
    mode = settings.sandbox
    image = image or settings.sandbox_image

    if mode == "k8s":
        from .k8s_runner import K8sSandbox

        return K8sSandbox(workspace, image)

    if mode in ("docker", "auto"):
        from .docker_runner import DockerSandbox, ImageMissing, docker_available

        if docker_available():
            try:
                return DockerSandbox(workspace, image)
            except ImageMissing:
                if mode == "docker":
                    raise
                # auto: image not built yet -> consider the local fallback
        elif mode == "docker":
            raise RuntimeError(
                "HARNESS_SANDBOX=docker but no Docker daemon is reachable. "
                "Start Docker or set HARNESS_SANDBOX=local."
            )
        # auto -> fall through to the trust-gated local fallback

    if not trusted and not settings.allow_local_untrusted:
        raise UntrustedSourceError(
            "Refusing to run an untrusted (remote) repository in the local "
            "sandbox: it provides no kernel or network isolation. Use the "
            "docker/k8s sandbox, or set HARNESS_ALLOW_LOCAL_UNTRUSTED=1 to "
            "accept the risk explicitly."
        )
    return LocalSandbox(workspace)
