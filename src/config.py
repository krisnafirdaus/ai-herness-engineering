"""Central configuration, resolved once from the environment.

Every tunable lives here so the rest of the codebase never reads ``os.environ``
directly. A ``.env`` file (if present) is loaded first; real environment
variables always win over ``.env`` values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader (no external dependency).

    Lines are ``KEY=VALUE``; ``#`` comments and blanks are ignored. Values
    already present in the real environment are NOT overwritten.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# Resolve repo root as the parent of the `src` package so paths are stable
# regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(REPO_ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ── LLM ──────────────────────────────────────────────────────────────────
    llm_provider: str = os.environ.get("HARNESS_LLM_PROVIDER", "mock").lower()
    llm_model: str = os.environ.get("HARNESS_LLM_MODEL", "").strip()
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")

    # ── Orchestration engine ─────────────────────────────────────────────────
    # langgraph = LangGraph StateGraph driver; builtin = stdlib while-loop;
    # auto = langgraph when installed, else builtin.
    orchestrator: str = os.environ.get("HARNESS_ORCHESTRATOR", "auto").lower()

    # ── Sandbox ──────────────────────────────────────────────────────────────
    sandbox: str = os.environ.get("HARNESS_SANDBOX", "auto").lower()
    sandbox_image: str = os.environ.get("HARNESS_SANDBOX_IMAGE", "harness-sandbox:latest")
    # Fail-closed policy: repos cloned from REMOTE URLs are untrusted and may
    # not run in the (non-kernel-isolated) local sandbox unless explicitly
    # allowed. Local-path repos are the operator's own code and stay allowed.
    allow_local_untrusted: bool = os.environ.get(
        "HARNESS_ALLOW_LOCAL_UNTRUSTED", "").strip().lower() in ("1", "true", "yes")
    # Optional `ulimit -u` for local sandbox commands (0 = leave unset; a
    # too-low value can break process-heavy test suites on shared machines).
    local_sandbox_nproc: int = _int("HARNESS_LOCAL_NPROC", 0)

    # ── GitHub (PR creation) ─────────────────────────────────────────────────
    github_token: str = (os.environ.get("HARNESS_GITHUB_TOKEN")
                         or os.environ.get("GITHUB_TOKEN", ""))
    github_api_url: str = os.environ.get("HARNESS_GITHUB_API_URL",
                                         "https://api.github.com")
    # Open a PR automatically when a GitHub-hosted run completes and a token
    # is configured. Set HARNESS_AUTO_PR=0 to disable.
    auto_pr: bool = os.environ.get("HARNESS_AUTO_PR", "1").strip() not in (
        "0", "false", "no", "")

    # ── State store ──────────────────────────────────────────────────────────
    database_url: str = os.environ.get(
        "HARNESS_DATABASE_URL", f"sqlite:///{REPO_ROOT / 'harness.sqlite3'}"
    )

    # ── Queue ────────────────────────────────────────────────────────────────
    queue: str = os.environ.get("HARNESS_QUEUE", "db").lower()
    redis_url: str = os.environ.get("HARNESS_REDIS_URL", "redis://localhost:6379/0")

    # ── Event streaming ──────────────────────────────────────────────────────
    # Push bus behind the SSE/WebSocket endpoints. auto = redis when the work
    # queue is redis (separate worker processes), else in-process fan-out.
    event_bus: str = os.environ.get("HARNESS_EVENT_BUS", "auto").lower()

    # ── Guardrails ───────────────────────────────────────────────────────────
    max_retries: int = _int("HARNESS_MAX_RETRIES", 3)
    max_tokens_per_run: int = _int("HARNESS_MAX_TOKENS_PER_RUN", 200_000)
    step_timeout_sec: int = _int("HARNESS_STEP_TIMEOUT_SEC", 600)

    # ── Telemetry ────────────────────────────────────────────────────────────
    langfuse_public_key: str = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key: str = os.environ.get("LANGFUSE_SECRET_KEY", "")
    langfuse_host: str = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

    # ── Paths ────────────────────────────────────────────────────────────────
    workspaces_root: Path = REPO_ROOT / "workspaces" / "runs"
    # When the worker runs INSIDE a container and launches sandbox containers via
    # the host Docker socket (docker-out-of-docker), bind-mount sources must be
    # HOST paths. This is the host path that maps to workspaces_root in-container;
    # set it in docker-compose. Empty => paths are already host paths (local dev).
    host_workspaces_root: str = os.environ.get("HARNESS_HOST_WORKSPACES_ROOT", "")

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


settings = Settings()
