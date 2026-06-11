"""Both orchestration engines (LangGraph + builtin) produce identical runs."""
import pytest

from src.orchestrator.graph import langgraph_available
from src.orchestrator.state_machine import StateMachine
from src.orchestrator.states import RunState, StepStatus
from src.storage.models import Repository

from conftest import PY_REPO, TASK

needs_langgraph = pytest.mark.skipif(
    not langgraph_available(), reason="langgraph not installed")


@pytest.mark.parametrize("engine", [
    pytest.param("langgraph", marks=needs_langgraph),
    "builtin",
])
def test_engine_completes_run_with_retry_loop(engine, override_settings):
    override_settings(orchestrator=engine)
    repo = Repository()
    run = repo.create_run(PY_REPO, TASK, "harness/test")
    result = StateMachine(repo).drive(run.run_id)

    assert result.status == RunState.COMPLETED.value
    steps = repo.get_steps(run.run_id)
    assert steps[0].status == StepStatus.COMPLETED.value
    assert steps[0].iterations == 1  # the verify-fail -> fix loop ran
    agents = {r["agent"] for r in repo.get_telemetry(run.run_id)}
    assert {"planner", "executor", "verifier"} <= agents


@needs_langgraph
def test_langgraph_resumes_mid_run_without_replanning(override_settings,
                                                      monkeypatch):
    """Crash inside the graph's verify node; re-drive resumes at verify."""
    override_settings(orchestrator="langgraph")
    repo = Repository()
    run_id = repo.create_run(PY_REPO, TASK, "harness/test").run_id

    from src.orchestrator import planner as planner_mod
    calls = {"plan": 0}
    real_plan = planner_mod.Planner.plan

    def counting_plan(self, *a, **k):
        calls["plan"] += 1
        return real_plan(self, *a, **k)

    monkeypatch.setattr(planner_mod.Planner, "plan", counting_plan)

    from src.orchestrator import verifier as verifier_mod
    real_verify = verifier_mod.Verifier.verify
    state = {"crashed": False}

    def crashing_verify(self, step):
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("simulated crash inside graph node")
        return real_verify(self, step)

    monkeypatch.setattr(verifier_mod.Verifier, "verify", crashing_verify)

    with pytest.raises(RuntimeError, match="simulated crash"):
        StateMachine(repo).drive(run_id)
    assert repo.get_run(run_id).status == RunState.VERIFYING_STEP.value

    resumed = StateMachine(repo).drive(run_id)
    assert resumed.status == RunState.COMPLETED.value
    assert calls["plan"] == 1  # plan cached; graph re-entered at verify


@needs_langgraph
def test_explicit_langgraph_mode_errors_without_package(override_settings,
                                                        monkeypatch):
    override_settings(orchestrator="langgraph")
    monkeypatch.setattr("src.orchestrator.graph.langgraph_available",
                        lambda: False)
    with pytest.raises(RuntimeError, match="langgraph"):
        StateMachine(Repository()).drive("run_whatever")
