# ADR 0003 — Patch-based executor edits (search/replace), not whole-file rewrites

**Status:** accepted

## Context

The Executor originally asked the LLM for the complete new file content and
overwrote the target. That is simple but wrong for production: it destroys
unrelated code when the model's reproduction of the file drifts, produces
unreviewable diffs, scales token cost with file size rather than edit size,
and turns every retry into a fresh chance to lose previously-correct content.

## Decision

The Executor contract (`EXECUTOR_SYSTEM` in `src/llm/prompts.py`) is an
ordered list of `{"find", "replace"}` operations:

* each `find` must be an **exact, unique** substring of the current file —
  ambiguous or missing anchors are rejected by `FileTools.search_replace`
  (uniqueness was already enforced there; the executor now actually uses it);
* whole-file `content` is accepted **only** for `create`;
* a model that violates the contract for `modify` falls back to a whole-file
  write that is loudly logged as a `WARN` event — progress beats a hard fail,
  but the violation is visible in the run log and stream;
* a patch that fails to apply raises a structured, **retryable** error: it is
  persisted as the step's `last_error` and routed through the same retry loop
  as a verification failure (new `EXECUTING_STEP → RETRYING_STEP` edge), so
  the model gets to correct its anchor instead of the run aborting.

## Consequences

* Diffs are minimal and reviewable (the PR body links to a surgical diff).
* A stale anchor consumes a retry rather than silently corrupting the file.
* The deterministic mock provider emits real patches too, so the offline
  demo exercises the same code path as production.
