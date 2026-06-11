"""LangGraph orchestration engine — the established-framework driver.

The run loop is expressed as a **LangGraph ``StateGraph``**: one node per
orchestration phase (prepare → plan → begin → execute → verify, plus the
rollback/finalize exits), with conditional edges that route on the *persisted*
run state after every node. The graph is the engine; the harness's own
SQLite/Postgres store stays the single source of durable truth:

* every node delegates to the same stage logic the built-in driver uses, and
  every transition is committed to the DB before the next node runs;
* the **conditional entry point** routes from the persisted status, so a
  crashed run resumes mid-graph (e.g. straight into ``verify``) without
  re-planning — identical recovery semantics to the built-in driver;
* we deliberately do NOT use LangGraph's checkpointer for durability: the
  state store predates the graph, is queryable by the API/CLI, and survives
  process death. The graph adds the orchestration framework (typed nodes,
  conditional routing, visualization via ``get_graph().draw_mermaid()``),
  not a second persistence layer to keep consistent.

Selection: ``HARNESS_ORCHESTRATOR=auto|langgraph|builtin`` (auto prefers
LangGraph when importable and falls back to the built-in while-loop driver,
which keeps the zero-dependency demo path alive).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from ..storage.models import Run
from ..telemetry.tracing import Tracer
from .states import RunState

if TYPE_CHECKING:  # pragma: no cover
    from .state_machine import StateMachine


class RunGraphState(TypedDict):
    run_id: str
    status: str


def langgraph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
        return True
    except ImportError:
        return False


# Persisted run status -> next graph node. END is handled in the router.
_ROUTE = {
    RunState.PENDING.value: "prepare",
    RunState.PLANNING.value: "plan",
    RunState.PLAN_READY.value: "begin",
    RunState.EXECUTING_STEP.value: "execute",
    RunState.RETRYING_STEP.value: "execute",
    RunState.VERIFYING_STEP.value: "verify",
    RunState.COMPLETED.value: "finalize",
    RunState.FAILED.value: "rollback",
}


class LangGraphDriver:
    """Drives one run to a terminal state through the compiled LangGraph."""

    def __init__(self, sm: "StateMachine") -> None:
        self.sm = sm

    def drive(self, run_id: str) -> Run:
        from langgraph.graph import END, StateGraph

        run = self.sm.repo.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")

        tracer = Tracer(self.sm.repo, run_id)
        sandbox_box: dict = {"sb": None}  # lazily attached, torn down in finally

        def _sandbox():
            if sandbox_box["sb"] is None:
                sandbox_box["sb"] = self.sm._attach_sandbox(
                    self.sm.repo.get_run(run_id))
            return sandbox_box["sb"]

        def _fresh_status() -> str:
            return self.sm.repo.get_run(run_id).status

        # ── nodes: thin wrappers over the shared stage logic ────────────────
        def prepare(state: RunGraphState) -> RunGraphState:
            self.sm._prepare(self.sm.repo.get_run(run_id))
            return {"run_id": run_id, "status": _fresh_status()}

        def plan(state: RunGraphState) -> RunGraphState:
            self.sm._plan(self.sm.repo.get_run(run_id), _sandbox(), tracer)
            return {"run_id": run_id, "status": _fresh_status()}

        def begin(state: RunGraphState) -> RunGraphState:
            self.sm._begin_first_step(self.sm.repo.get_run(run_id))
            return {"run_id": run_id, "status": _fresh_status()}

        def execute(state: RunGraphState) -> RunGraphState:
            self.sm._execute(self.sm.repo.get_run(run_id), _sandbox(), tracer)
            return {"run_id": run_id, "status": _fresh_status()}

        def verify(state: RunGraphState) -> RunGraphState:
            self.sm._verify(self.sm.repo.get_run(run_id), _sandbox(), tracer)
            return {"run_id": run_id, "status": _fresh_status()}

        def rollback(state: RunGraphState) -> RunGraphState:
            self.sm._rollback(self.sm.repo.get_run(run_id))
            return {"run_id": run_id, "status": _fresh_status()}

        def finalize(state: RunGraphState) -> RunGraphState:
            self.sm._finalize(self.sm.repo.get_run(run_id))
            return {"run_id": run_id, "status": _fresh_status()}

        def route(state: RunGraphState):
            # The DB is authoritative — never trust in-graph state for control
            # flow. ROLLED_BACK (and any unexpected terminal) ends the graph.
            return _ROUTE.get(_fresh_status(), END)

        g = StateGraph(RunGraphState)
        for name, fn in [("prepare", prepare), ("plan", plan), ("begin", begin),
                         ("execute", execute), ("verify", verify),
                         ("rollback", rollback), ("finalize", finalize)]:
            g.add_node(name, fn)

        route_targets = {v: v for v in _ROUTE.values()} | {END: END}
        g.set_conditional_entry_point(route, route_targets)
        for name in ("prepare", "plan", "begin", "execute", "verify"):
            g.add_conditional_edges(name, route, route_targets)
        g.add_edge("rollback", END)
        g.add_edge("finalize", END)

        compiled = g.compile()
        try:
            # Recursion limit bounds graph hops; a run of S steps with R
            # retries needs ~3*S*(R+1) hops. 1000 is far above any budgeted
            # run and still fails fast on a routing bug.
            compiled.invoke({"run_id": run_id, "status": run.status},
                            config={"recursion_limit": 1000})
            return self.sm.repo.get_run(run_id)
        finally:
            if sandbox_box["sb"] is not None:
                sandbox_box["sb"].teardown()
