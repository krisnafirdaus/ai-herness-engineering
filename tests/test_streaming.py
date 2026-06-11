"""Push-based event streaming: bus fan-out, persistence hook, SSE generator."""
import json
import threading
import time

from src.events import InProcessBus, get_bus, reset_bus
from src.orchestrator.states import RunState
from src.storage.models import Repository


def test_inprocess_bus_fans_out_and_unsubscribes():
    bus = InProcessBus()
    a, b = bus.subscribe("run_1"), bus.subscribe("run_1")
    other = bus.subscribe("run_2")
    bus.publish("run_1", {"id": 1})
    assert a.get(0.1) == {"id": 1}
    assert b.get(0.1) == {"id": 1}
    assert other.get(0.05) is None
    a.close()
    bus.publish("run_1", {"id": 2})
    assert b.get(0.1) == {"id": 2}


def test_add_event_publishes_to_live_subscribers():
    reset_bus()
    repo = Repository()
    run = repo.create_run("./dummy-repos/python-api-sample", "t", "b")
    sub = get_bus().subscribe(run.run_id)
    try:
        event_id = repo.add_event(run.run_id, "INFO", "hello stream",
                                  stage="PENDING")
        ev = sub.get(timeout=1.0)
        assert ev and ev["id"] == event_id and ev["message"] == "hello stream"
    finally:
        sub.close()


def test_sse_stream_replays_then_pushes_live_then_ends():
    from src.api.server import _event_stream

    reset_bus()
    repo = Repository()
    run = repo.create_run("./dummy-repos/python-api-sample", "t", "b")
    repo.add_event(run.run_id, "INFO", "first", stage="PENDING")
    repo.add_event(run.run_id, "INFO", "second", stage="PLANNING")

    def finish_run():
        time.sleep(0.3)
        repo2 = Repository()
        repo2.add_event(run.run_id, "INFO", "live event", stage="EXECUTING_STEP")
        r = repo2.get_run(run.run_id)
        r.status = RunState.COMPLETED.value
        repo2.update_run(r)
        repo2.add_event(run.run_id, "INFO", "all steps verified",
                        stage="COMPLETED")

    threading.Thread(target=finish_run, daemon=True).start()

    frames = list(_event_stream(run.run_id, 0))
    blob = "".join(frames)

    assert frames[0].startswith("event: snapshot\n")
    assert blob.index("first") < blob.index("second") < blob.index("live event")
    assert "all steps verified" in blob
    assert frames[-1].startswith("event: end\n")
    end_payload = json.loads(frames[-1].split("data: ", 1)[1])
    assert end_payload["status"] == "COMPLETED"
    # Dedup: every event id appears exactly once.
    ids = [json.loads(f.split("data: ", 1)[1])["id"]
           for f in frames if f.startswith("id: ")]
    assert len(ids) == len(set(ids))


def test_sse_resume_skips_already_seen_events():
    from src.api.server import _event_stream

    reset_bus()
    repo = Repository()
    run = repo.create_run("./dummy-repos/python-api-sample", "t", "b")
    first = repo.add_event(run.run_id, "INFO", "seen already", stage="PENDING")
    repo.add_event(run.run_id, "INFO", "new for client", stage="PLANNING")
    r = repo.get_run(run.run_id)
    r.status = RunState.COMPLETED.value
    repo.update_run(r)

    blob = "".join(_event_stream(run.run_id, first))
    assert "seen already" not in blob
    assert "new for client" in blob


def test_stream_endpoint_404_for_unknown_run():
    from fastapi.testclient import TestClient

    from src.api.server import app

    with TestClient(app) as client:
        assert client.get("/runs/run_nope/stream").status_code == 404
