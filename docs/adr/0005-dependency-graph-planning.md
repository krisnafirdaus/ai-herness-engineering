# ADR 0005 — Static import graph for planner dependency analysis

**Status:** accepted

## Context

The Planner's "dependency hints" used to be project-shape booleans
(`has_package_json`, test-file names). The LLM had to guess edit order from
file names, and nothing validated the order it chose. For multi-file
refactors that guess is exactly where runs go wrong: edit the caller before
the callee and every intermediate verification fails for a reason the model
didn't cause.

## Decision

`src/analysis/dep_graph.py` builds a real intra-repo import graph before
planning:

* **Python** via `ast`: absolute imports, relative imports (`from . import
  x`, `from ..pkg import y`), package `__init__` resolution,
  `from pkg import name`-as-module;
* **JS/TS** via import/require scanning with standard extension and
  `/index.*` probing; only relative specifiers — external packages are not
  part of the graph;
* forward edges (`imports_of`), reverse edges (`dependents_of` — the blast
  radius of editing a file), transitive dependents, cycle detection, and a
  stable topological sort.

The Planner (a) embeds each candidate file's `imports`/`imported_by` and any
cycles into the prompt, (b) ranks snippet candidates partly by
dependents-centrality so structurally load-bearing files are shown to the
model, (c) accepts an explicit `depends_on` per step, validated against the
plan, and (d) **topologically re-orders the final steps** using declared
edges ∪ import edges — the model's array order is a tie-breaker, not the
schedule. Cycles degrade to the model's order instead of failing the plan.
`depends_on` is persisted per step (`steps.depends_on_json`).

## Why not a full language server / call graph?

Import granularity is what step ordering needs (steps are per-file). A call
graph would be more precise and far heavier (per-language tooling, indexing
time) without changing the schedule for per-file steps. The module is
deliberately side-effect free (`read_file` injected) so a richer analyzer can
replace it behind the same interface.

## Consequences

* Step order is structurally correct even when the LLM's isn't; the
  declared-dependency escape hatch covers non-import relationships (configs,
  codegen).
* Planning cost grows by one parse pass over source files, bounded at 400 KB
  per file.
