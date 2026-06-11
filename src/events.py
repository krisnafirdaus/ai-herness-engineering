"""Run-event bus: push-based fan-out powering the streaming endpoints.

Every state transition / log line written through ``Repository.add_event`` is
*published* here in addition to being persisted. Subscribers (the SSE and
WebSocket endpoints) receive events as they happen — no client polling.

Two backends behind one interface:

* ``memory`` — in-process fan-out (``queue.Queue`` per subscriber, thread-safe).
  Correct whenever producer and consumer share a process: the single-node API
  mode where runs are driven in a background task, the CLI, and tests.
* ``redis``  — Redis pub/sub on ``harness:events:{run_id}``, for the
  production topology where workers and API servers are separate processes
  (and separate machines).

``auto`` picks redis when the work queue is redis (separate workers implied),
else memory. Delivery is best-effort by design: the DB row is the durable
record, so a dropped pub/sub message can always be recovered from the events
table (the SSE endpoint does exactly that — replay + live merge, deduped by
event id).
"""
from __future__ import annotations

import json
import queue
import threading

from .config import settings


class Subscription:
    """Handle for one subscriber. ``get`` blocks up to ``timeout`` seconds."""

    def get(self, timeout: float = 1.0) -> dict | None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        raise NotImplementedError


class EventBus:
    def publish(self, run_id: str, event: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    def subscribe(self, run_id: str) -> Subscription:  # pragma: no cover
        raise NotImplementedError


# ── in-process backend ────────────────────────────────────────────────────────
class _MemorySubscription(Subscription):
    def __init__(self, bus: "InProcessBus", run_id: str) -> None:
        self._bus = bus
        self._run_id = run_id
        self.q: queue.Queue = queue.Queue()

    def get(self, timeout: float = 1.0) -> dict | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._bus._drop(self._run_id, self)


class InProcessBus(EventBus):
    def __init__(self) -> None:
        self._subs: dict[str, list[_MemorySubscription]] = {}
        self._lock = threading.Lock()

    def publish(self, run_id: str, event: dict) -> None:
        with self._lock:
            subs = list(self._subs.get(run_id, ()))
        for sub in subs:
            sub.q.put_nowait(event)

    def subscribe(self, run_id: str) -> Subscription:
        sub = _MemorySubscription(self, run_id)
        with self._lock:
            self._subs.setdefault(run_id, []).append(sub)
        return sub

    def _drop(self, run_id: str, sub: _MemorySubscription) -> None:
        with self._lock:
            lst = self._subs.get(run_id, [])
            if sub in lst:
                lst.remove(sub)
            if not lst:
                self._subs.pop(run_id, None)


# ── redis backend ─────────────────────────────────────────────────────────────
def _channel(run_id: str) -> str:
    return f"harness:events:{run_id}"


class _RedisSubscription(Subscription):
    def __init__(self, redis_client, run_id: str) -> None:
        self._pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        self._pubsub.subscribe(_channel(run_id))

    def get(self, timeout: float = 1.0) -> dict | None:
        msg = self._pubsub.get_message(timeout=timeout)
        if not msg or msg.get("type") != "message":
            return None
        try:
            return json.loads(msg["data"])
        except (TypeError, ValueError):
            return None

    def close(self) -> None:
        try:
            self._pubsub.unsubscribe()
            self._pubsub.close()
        except Exception:
            pass


class RedisBus(EventBus):
    def __init__(self) -> None:
        import redis

        self._r = redis.Redis.from_url(settings.redis_url)

    def publish(self, run_id: str, event: dict) -> None:
        try:
            self._r.publish(_channel(run_id), json.dumps(event))
        except Exception:
            # Best-effort: the DB row is the durable record; SSE replay
            # recovers anything pub/sub drops.
            pass

    def subscribe(self, run_id: str) -> Subscription:
        return _RedisSubscription(self._r, run_id)


# ── selection ─────────────────────────────────────────────────────────────────
_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_bus() -> EventBus:
    """Process-wide bus singleton, selected by ``HARNESS_EVENT_BUS``."""
    global _bus
    if _bus is not None:
        return _bus
    with _bus_lock:
        if _bus is None:
            mode = settings.event_bus
            if mode == "auto":
                mode = "redis" if settings.queue == "redis" else "memory"
            if mode == "redis":
                try:
                    _bus = RedisBus()
                except Exception:
                    _bus = InProcessBus()  # degrade rather than break runs
            else:
                _bus = InProcessBus()
    return _bus


def reset_bus() -> None:
    """Test hook: force re-selection of the bus backend."""
    global _bus
    with _bus_lock:
        _bus = None
