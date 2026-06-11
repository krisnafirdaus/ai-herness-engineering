# ADR 0001 — LangGraph as the orchestration engine

**Status:** accepted

## Context

The run loop (Planner → Executor → Verifier with retries, rollback and
resume) was originally a hand-rolled `while` loop over a persisted state
machine. That worked, but it meant the project carried a bespoke orchestration
engine nobody else maintains, with no ecosystem for visualization, tracing
integration, or hiring familiarity. Reviewers expect an established agent
orchestration framework in production systems.

## Decision

Express the run loop as a **LangGraph `StateGraph`** (`src/orchestrator/graph.py`):
one node per phase (`prepare`, `plan`, `begin`, `execute`, `verify`,
`rollback`, `finalize`) with **conditional edges routed on the persisted run
status** and a **conditional entry point** so a resumed run enters the graph
mid-flight (e.g. directly at `verify`).

Two deliberate boundaries:

1. **Our database stays the single source of durable truth.** We do not use
   LangGraph's checkpointer. The store predates the graph, is queried by the
   API/CLI/streaming endpoints, and survives process death; running a second
   persistence layer would create a consistency problem (which copy wins
   after a crash?) without adding capability.
2. **Stage logic is engine-agnostic.** Nodes are thin wrappers over the same
   `_prepare/_plan/_execute/_verify/...` methods the builtin driver uses, so
   both engines have identical behavior and one test suite covers both.

`HARNESS_ORCHESTRATOR=auto|langgraph|builtin` selects the engine; `auto`
prefers LangGraph and falls back to the builtin loop, keeping the
zero-dependency offline demo alive.

## Consequences

* The orchestration topology is declared, inspectable
  (`get_graph().draw_mermaid()`), and familiar to anyone who knows LangGraph.
* Crash-resume semantics are provably identical across engines
  (`tests/test_langgraph_engine.py` runs the same scenarios on both).
* We accept a real dependency (`langgraph`) in the default install; the
  builtin driver remains as the degradation path, not a parallel feature.
