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


def select_sandbox(workspace: str, image: str | None = None) -> Sandbox:
    """Instantiate the configured sandbox for a run's workspace.

    ``auto`` (default) prefers Docker and silently falls back to local when the
    daemon is unreachable, so a demo never hard-fails on a missing daemon.
    """
    mode = settings.sandbox
    image = image or settings.sandbox_image

    if mode in ("docker", "auto"):
        from .docker_runner import DockerSandbox, ImageMissing, docker_available

        if docker_available():
            try:
                return DockerSandbox(workspace, image)
            except ImageMissing:
                if mode == "docker":
                    raise
                # auto: image not built yet -> degrade to local rather than fail
        elif mode == "docker":
            raise RuntimeError(
                "HARNESS_SANDBOX=docker but no Docker daemon is reachable. "
                "Start Docker or set HARNESS_SANDBOX=local."
            )
        # auto -> fall back
    return LocalSandbox(workspace)
