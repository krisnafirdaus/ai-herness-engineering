"""Gates for the integration suite.

Every test here exercises a REAL dependency (Docker daemon, Redis, Postgres,
the network, an LLM provider, a k8s cluster). Tests are skipped — with the
reason printed — when their dependency is not available, so `make test` stays
green anywhere while `make test-integration` (backed by
infra/docker-compose.test.yml) runs the full matrix.
"""
from __future__ import annotations

import os
import socket

import pytest

# ── probes (cached per session) ───────────────────────────────────────────────


def _docker_daemon() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


def docker_image() -> str | None:
    """A locally-present image suitable for sandbox exec tests."""
    if not _docker_daemon():
        return None
    import docker

    client = docker.from_env()
    for image in ("harness-sandbox:latest", "python:3.12-alpine",
                  "python:3.11-alpine", "alpine:latest"):
        try:
            client.images.get(image)
            return image
        except Exception:
            continue
    return None


def redis_url() -> str | None:
    url = os.environ.get("HARNESS_TEST_REDIS_URL", "redis://localhost:6379/15")
    try:
        import redis

        redis.Redis.from_url(url, socket_connect_timeout=1).ping()
        return url
    except Exception:
        return None


def postgres_url() -> str | None:
    url = os.environ.get("HARNESS_TEST_POSTGRES_URL", "")
    if not url:
        return None
    try:
        import psycopg

        psycopg.connect(url, connect_timeout=2).close()
        return url
    except Exception:
        return None


def network_available(host: str = "github.com", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def k8s_enabled() -> bool:
    if os.environ.get("HARNESS_TEST_K8S") != "1":
        return False
    try:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        client.CoreV1Api().list_namespace(limit=1)
        return True
    except Exception:
        return False


# ── shared fixtures ───────────────────────────────────────────────────────────
@pytest.fixture
def fresh_db(override_settings, tmp_path):
    """Point the store at a throwaway SQLite DB and reset cached connections."""
    from src.storage.db import reset_connections

    override_settings(database_url=f"sqlite:///{tmp_path}/integration.sqlite3")
    reset_connections()
    yield
    reset_connections()
