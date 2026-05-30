"""Run queue abstraction used by the API (producer) and worker (consumer).

Two backends:

* ``db``    — no external broker. Producers just leave the run in ``PENDING``;
              workers discover it via an atomic ``claim_pending`` UPDATE. Simple
              and crash-safe (state IS the queue), good for single-node/demo.
* ``redis`` — a Redis list as the work queue for horizontal worker pools. The DB
              is still the source of truth; Redis only carries run ids, and the
              worker re-scans the DB for resumable runs on startup so a lost
              message never strands a run.
"""
from __future__ import annotations

from .config import settings


class Queue:
    def enqueue(self, run_id: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def dequeue(self, timeout: int = 5) -> str | None:  # pragma: no cover
        raise NotImplementedError


class DBQueue(Queue):
    """The state store is the queue; enqueue is a no-op (run is already PENDING)."""

    def enqueue(self, run_id: str) -> None:
        return None

    def dequeue(self, timeout: int = 5) -> str | None:
        from .storage.models import Repository

        run = Repository().claim_pending()
        return run.run_id if run else None


class RedisQueue(Queue):
    KEY = "harness:runs"

    def __init__(self) -> None:
        import redis

        self._r = redis.Redis.from_url(settings.redis_url)

    def enqueue(self, run_id: str) -> None:
        self._r.rpush(self.KEY, run_id)

    def dequeue(self, timeout: int = 5) -> str | None:
        item = self._r.blpop(self.KEY, timeout=timeout)
        return item[1].decode() if item else None


def get_queue() -> Queue:
    return RedisQueue() if settings.queue == "redis" else DBQueue()
