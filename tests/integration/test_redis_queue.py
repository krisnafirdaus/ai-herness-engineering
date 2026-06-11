"""RedisQueue + RedisBus against a real Redis."""
import pytest

from integration.conftest import redis_url

_URL = redis_url()
pytestmark = [
    pytest.mark.integration, pytest.mark.redis,
    pytest.mark.skipif(_URL is None, reason="no reachable Redis "
                       "(set HARNESS_TEST_REDIS_URL or run docker-compose.test)"),
]


@pytest.fixture
def redis_settings(override_settings):
    override_settings(redis_url=_URL, queue="redis", event_bus="redis")
    from src.events import reset_bus

    reset_bus()
    yield
    reset_bus()


def test_queue_round_trip(redis_settings):
    from src.queue import RedisQueue

    q = RedisQueue()
    q.enqueue("run_integration_1")
    q.enqueue("run_integration_2")
    assert q.dequeue(timeout=2) == "run_integration_1"   # FIFO
    assert q.dequeue(timeout=2) == "run_integration_2"
    assert q.dequeue(timeout=1) is None                  # drained


def test_event_bus_pub_sub_round_trip(redis_settings):
    from src.events import RedisBus

    bus = RedisBus()
    sub = bus.subscribe("run_integration_bus")
    try:
        # Subscription needs a beat to register before the publish.
        import time

        time.sleep(0.2)
        bus.publish("run_integration_bus",
                    {"id": 1, "message": "hello over redis"})
        ev = None
        deadline = time.time() + 5
        while ev is None and time.time() < deadline:
            ev = sub.get(timeout=1.0)
        assert ev and ev["message"] == "hello over redis"
    finally:
        sub.close()
