"""Behavioural tests for the orchestrator: the loop, retries, rollback, resume."""
from src.orchestrator.state_machine import StateMachine
from src.orchestrator.states import RunState, StepStatus
from src.storage.models import Repository

from conftest import PY_REPO, TASK


def _new_run(repo: Repository):
    return repo.create_run(PY_REPO, TASK, "harness/test")


def test_happy_path_completes_with_one_retry():
    """Mock executor's first attempt fails verification; the retry fixes it."""
    repo = Repository()
    run = StateMachine(repo).drive(_new_run(repo).run_id)

    assert run.status == RunState.COMPLETED.value
    steps = repo.get_steps(run.run_id)
    assert len(steps) == 1
    assert steps[0].status == StepStatus.COMPLETED.value
    assert steps[0].iterations == 1          # exactly one verify-fail -> fix cycle
    assert run.tokens_used > 0               # telemetry accounted for tokens


def test_telemetry_breakdown_has_planner_executor_verifier():
    repo = Repository()
    run = StateMachine(repo).drive(_new_run(repo).run_id)
    agents = {r["agent"] for r in repo.get_telemetry(run.run_id)}
    assert {"planner", "executor", "verifier"} <= agents


def test_rollback_on_exhausted_retries(override_settings):
    """With zero retry budget the buggy first attempt fails -> FAIL -> ROLLED_BACK."""
    override_settings(max_retries=0)
    repo = Repository()
    run = StateMachine(repo).drive(_new_run(repo).run_id)

    assert run.status == RunState.ROLLED_BACK.value
    assert "failed after 0 retries" in (run.error or "")
    # Workspace was reset to base: no surviving edits.
    from src.git import RepoManager
    assert RepoManager(run.workspace_path).changed_files(run.base_ref) == []


def test_token_budget_guardrail_trips(override_settings):
    override_settings(max_tokens_per_run=1)   # any real call blows the budget
    repo = Repository()
    run = StateMachine(repo).drive(_new_run(repo).run_id)
    assert run.status == RunState.ROLLED_BACK.value
    assert "token budget exceeded" in (run.error or "")


def test_crash_recovery_resumes_without_replanning(monkeypatch):
    """Simulate a crash during verification; resume must NOT re-run the Planner
    and must finish the run from persisted state."""
    repo = Repository()
    run_id = _new_run(repo).run_id

    # Count planner invocations.
    from src.orchestrator import planner as planner_mod
    calls = {"plan": 0}
    real_plan = planner_mod.Planner.plan

    def counting_plan(self, *a, **k):
        calls["plan"] += 1
        return real_plan(self, *a, **k)

    monkeypatch.setattr(planner_mod.Planner, "plan", counting_plan)

    # Crash exactly once, the first time we reach verification.
    from src.orchestrator import verifier as verifier_mod
    real_verify = verifier_mod.Verifier.verify
    state = {"crashed": False}

    def crashing_verify(self, step):
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("simulated worker crash mid-verification")
        return real_verify(self, step)

    monkeypatch.setattr(verifier_mod.Verifier, "verify", crashing_verify)

    # First drive crashes out.
    try:
        StateMachine(repo).drive(run_id)
        assert False, "expected simulated crash"
    except RuntimeError:
        pass

    mid = repo.get_run(run_id)
    assert mid.status == RunState.VERIFYING_STEP.value   # persisted mid-flight
    assert mid.plan_json is not None

    # Resume from persisted state (fresh state machine, as a restarted worker).
    resumed = StateMachine(repo).drive(run_id)
    assert resumed.status == RunState.COMPLETED.value
    assert calls["plan"] == 1                            # planner NOT called again


def test_recover_picks_up_nonterminal_runs():
    repo = Repository()
    r = _new_run(repo)
    # A brand-new PENDING run is resumable.
    ids = {x.run_id for x in repo.list_resumable()}
    assert r.run_id in ids
    StateMachine(repo).drive(r.run_id)
    ids_after = {x.run_id for x in repo.list_resumable()}
    assert r.run_id not in ids_after                     # terminal now
