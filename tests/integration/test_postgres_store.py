"""The state store against a real Postgres (dialect adaptation + migrations)."""
import pytest

from integration.conftest import postgres_url

_URL = postgres_url()
pytestmark = [
    pytest.mark.integration, pytest.mark.postgres,
    pytest.mark.skipif(_URL is None, reason="no reachable Postgres "
                       "(set HARNESS_TEST_POSTGRES_URL or run docker-compose.test)"),
]


@pytest.fixture
def pg_repo(override_settings):
    from src.storage.db import reset_connections

    override_settings(database_url=_URL)
    reset_connections()
    from src.storage.models import Repository

    repo = Repository()
    yield repo
    # Clean up integration rows, then drop the cached connection.
    for run in repo.list_runs():
        if run.task.startswith("pg-integration"):
            repo.delete_run(run.run_id)
    reset_connections()


def test_schema_and_migrations_are_idempotent(pg_repo):
    from src.storage.db import init_db

    init_db()
    init_db()  # second call must be a no-op, including ALTER TABLE migrations


def test_run_step_event_round_trip(pg_repo):
    from src.storage.models import Repository, Step

    run = pg_repo.create_run("https://github.com/o/r", "pg-integration task", "b")
    pg_repo.replace_steps(run.run_id, [Step(
        run_id=run.run_id, step_index=0, step_id="step-1", file="a.py",
        action="modify", reason="r", checks=["true"], depends_on=["b.py"],
    )])
    event_id = pg_repo.add_event(run.run_id, "INFO", "hello pg",
                                 stage="PENDING", data={"k": "v"})

    fresh = Repository()  # same thread-local conn; exercises row mapping
    loaded = fresh.get_run(run.run_id)
    assert loaded and loaded.task == "pg-integration task"
    steps = fresh.get_steps(run.run_id)
    assert steps[0].depends_on == ["b.py"]
    assert event_id > 0
    assert any(e["message"] == "hello pg"
               for e in fresh.get_events_since(run.run_id, 0))


def test_claim_pending_is_atomic(pg_repo):
    run = pg_repo.create_run("https://github.com/o/r", "pg-integration claim", "b")
    claimed = pg_repo.claim_pending()
    assert claimed and claimed.status == "PLANNING"
    # A second claim cannot return the same run.
    second = pg_repo.claim_pending()
    assert second is None or second.run_id != claimed.run_id
    assert run.run_id in (claimed.run_id, getattr(second, "run_id", None)) \
        or pg_repo.get_run(run.run_id).status == "PLANNING"


def test_cascade_delete(pg_repo):
    run = pg_repo.create_run("https://github.com/o/r", "pg-integration cascade", "b")
    pg_repo.add_telemetry(run.run_id, "planner", input_tokens=1)
    pg_repo.add_event(run.run_id, "INFO", "x")
    pg_repo.delete_run(run.run_id)
    assert pg_repo.get_run(run.run_id) is None
    assert pg_repo.get_telemetry(run.run_id) == []
    assert pg_repo.get_events(run.run_id) == []
