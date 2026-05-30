"""Orchestration layer: the persistent state machine and the three agent stages.

    Planner  (read-only)  -> strict-JSON execution plan
    Executor (sandboxed)  -> applies one step's file edits
    Verifier (sandboxed)  -> runs test + lint checks, feeds errors back

The :class:`~src.orchestrator.state_machine.StateMachine` is the single driver:
it is *resumable* — every transition is persisted before the next begins, so a
crash at any point is recovered by re-driving the run from its stored state.
"""
