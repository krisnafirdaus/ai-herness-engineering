"""Run- and step-level state vocabularies, plus transition rules.

These are intentionally explicit (string-valued enums persisted verbatim) so a
run row is human-readable in the DB and a crashed run can be reasoned about by
eye. The ``RunState`` graph is the contract the worker resumes against.
"""
from __future__ import annotations

from enum import Enum


class RunState(str, Enum):
    PENDING = "PENDING"               # accepted, workspace not yet prepared
    PLANNING = "PLANNING"             # Planner is (or should be) producing a plan
    PLAN_READY = "PLAN_READY"         # plan persisted, no step started yet
    EXECUTING_STEP = "EXECUTING_STEP" # Executor applying the current step
    VERIFYING_STEP = "VERIFYING_STEP" # Verifier running checks for current step
    RETRYING_STEP = "RETRYING_STEP"   # last verify failed, Executor will fix
    COMPLETED = "COMPLETED"           # all steps verified green (terminal)
    FAILED = "FAILED"                 # gave up (terminal, pre-rollback)
    ROLLED_BACK = "ROLLED_BACK"       # changes reverted after failure (terminal)

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_resumable(self) -> bool:
        """States a restarted worker may legitimately pick up mid-flight."""
        return self in _RESUMABLE


_TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.ROLLED_BACK}

# A worker that restarts after a crash looks for runs in ANY non-terminal state.
# PENDING/PLANNING re-enter cleanly; the *_STEP states resume from the persisted
# current_step + iteration without re-invoking the Planner.
_RESUMABLE = {
    RunState.PENDING,
    RunState.PLANNING,
    RunState.PLAN_READY,
    RunState.EXECUTING_STEP,
    RunState.VERIFYING_STEP,
    RunState.RETRYING_STEP,
}

# Allowed forward transitions. Enforced by the state machine so an illegal jump
# (e.g. EXECUTING_STEP -> COMPLETED without verifying) raises instead of silently
# corrupting a run.
ALLOWED_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.PENDING: {RunState.PLANNING, RunState.FAILED},
    RunState.PLANNING: {RunState.PLAN_READY, RunState.FAILED},
    RunState.PLAN_READY: {RunState.EXECUTING_STEP, RunState.COMPLETED, RunState.FAILED},
    RunState.EXECUTING_STEP: {RunState.VERIFYING_STEP, RunState.FAILED},
    RunState.VERIFYING_STEP: {
        RunState.EXECUTING_STEP,  # advance to next step
        RunState.RETRYING_STEP,   # this step failed, retry budget remains
        RunState.COMPLETED,       # last step passed
        RunState.FAILED,          # retry budget exhausted / token budget blown
    },
    RunState.RETRYING_STEP: {RunState.VERIFYING_STEP, RunState.FAILED},
    RunState.FAILED: {RunState.ROLLED_BACK},
    RunState.COMPLETED: set(),
    RunState.ROLLED_BACK: set(),
}


class StepStatus(str, Enum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StepAction(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
