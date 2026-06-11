"""Full HTTP loop through the FastAPI control plane (real ASGI transport).

Runs anywhere fastapi+httpx are installed: the mock provider + local sandbox
make the run itself hermetic, so this exercises the API surface end-to-end —
create -> drive -> status -> stream (SSE) -> traces -> events -> diff.
"""
import json

import pytest

from conftest import PY_REPO, TASK

pytestmark = [pytest.mark.integration]

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def client(fresh_db):
    from fastapi.testclient import TestClient

    from src.api.server import app

    with TestClient(app) as c:
        yield c


def test_full_run_lifecycle_over_http(client):
    # POST /runs — in db-queue mode the run is driven as a background task,
    # which the test client executes before returning the response.
    resp = client.post("/runs", json={"repo": PY_REPO, "task": TASK})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    # GET /runs/{id}
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "COMPLETED"
    assert run["total_steps"] == 1
    assert run["steps"][0]["iterations"] == 1     # the self-fix loop ran

    # GET /runs/{id}/traces
    traces = client.get(f"/runs/{run_id}/traces").json()
    assert traces["summary"]["total_tokens"] > 0
    assert {"planner", "executor", "verifier"} <= set(
        traces["summary"]["by_agent"])

    # GET /runs/{id}/events
    events = client.get(f"/runs/{run_id}/events").json()["events"]
    assert any("verified green" in e["message"] for e in events)

    # GET /runs/{id}/diff
    diff = client.get(f"/runs/{run_id}/diff").json()["diff"]
    assert "validate_payload" in diff

    # GET /runs — list contains it
    assert any(r["run_id"] == run_id
               for r in client.get("/runs").json()["runs"])


def test_sse_stream_delivers_replay_and_end_frame(client):
    run_id = client.post("/runs", json={"repo": PY_REPO, "task": TASK}).json()["run_id"]

    frames = []
    with client.stream("GET", f"/runs/{run_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            frames.append(line)
            if line.startswith("event: end"):
                break

    blob = "\n".join(frames)
    assert "event: snapshot" in blob
    assert "verified green" in blob
    data_lines = [l for l in frames if l.startswith("data: ")]
    snapshot = json.loads(data_lines[0][len("data: "):])
    assert snapshot["status"] == "COMPLETED"


def test_resume_endpoint_reenqueues(client):
    run_id = client.post("/runs", json={"repo": PY_REPO, "task": TASK}).json()["run_id"]
    resp = client.post(f"/runs/{run_id}/resume")
    assert resp.status_code == 202 and resp.json()["resuming"] is True


def test_pr_endpoint_rejects_non_github_run(client):
    run_id = client.post("/runs", json={"repo": PY_REPO, "task": TASK}).json()["run_id"]
    resp = client.post(f"/runs/{run_id}/pr")
    assert resp.status_code == 409
    assert "not a GitHub URL" in resp.json()["detail"]


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
