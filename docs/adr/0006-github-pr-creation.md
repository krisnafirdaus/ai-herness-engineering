# ADR 0006 — GitHub PR creation: stdlib REST + env-based push auth

**Status:** accepted

## Context

A completed run used to stop at "committed on branch — PR ready". The
assessment requires actually opening the pull request.

## Decision

`src/git/github.py`:

* **Push**: to an explicit `https://github.com/{owner}/{repo}.git` URL with
  the token supplied via `GIT_CONFIG_*` **environment variables**
  (`http.extraHeader` Basic credential). The token never appears in argv
  (process listings) or in `.git/config`/remotes (workspace is
  retention-swept, but still). Shallow clones are `--unshallow`ed on demand
  because GitHub rejects shallow pushes.
* **PR**: GitHub REST v3 via stdlib `urllib` — one endpoint did not justify
  an SDK dependency; the transport is injectable so unit tests run a scripted
  fake and the real call is integration-tested. Creation is **idempotent**:
  422 "already exists" resolves to the existing open PR.
* **Wiring**: on `COMPLETED`, the state machine auto-opens a PR when the run
  came from a GitHub URL and a token is configured (`HARNESS_AUTO_PR`,
  default on). A PR failure is an `ERROR` event, never a failed run — the
  refactor itself succeeded; the operator can re-trigger with
  `python3 -m src.main pr --run-id …` or `POST /runs/{id}/pr`. `runs.pr_url`
  persists the result and the second call short-circuits.

## Consequences

* The full assessment loop closes: clone → plan → patch → verify → commit →
  **PR**.
* Local-path runs (no remote) skip PR creation with an explanatory event
  instead of erroring.
