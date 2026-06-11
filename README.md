# AI Agent Harness — Autonomous Multi-File Refactoring Runner


A **production-shaped harness** that takes a public GitHub repo + a task, then
drives three agents — **Planner → Executor → Verifier** — on a **LangGraph
`StateGraph`** to plan a refactor over a static import-dependency graph, apply
**patch-based edits** (exact search/replace, never whole-file rewrites) in an
isolated sandbox, run the repo's tests/linters, **fix its own failures**, roll
back after 3 failed attempts — and **open a GitHub pull request** when green.
Every transition is persisted *and pushed live over SSE/WebSocket*, so a worker
crash at step 4 of 10 resumes from step 4 — without re-planning or re-paying
for verified steps.

> The emphasis is the *system*: orchestration, safety, resumability, sandboxing,
> the verification loop, telemetry, and a deployment blueprint — not the prompt.

> **Deep dive:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) walks one run
> end-to-end (life of a run, failure-mode table, security model), and
> [docs/adr/](docs/adr/) holds eight ADRs recording each major decision and the
> trade-offs behind it.

```
        repo + task
            │
            ▼
   ┌──────────────────┐     strict-JSON plan      ┌──────────────────────────┐
   │   PLANNER         │ ─────────────────────────▶│  persistent state store  │
   │  (read-only)      │                           │  runs / steps / errors / │
   └──────────────────┘                            │  telemetry  (SQLite/PG)  │
            │                                       └──────────────────────────┘
            ▼                                                  ▲
   ┌──────────────────┐   patch    ┌─────────────┐            │ every transition
   │   EXECUTOR        │ ─────────▶ │   SANDBOX    │           │ persisted before
   │  (one step)       │           │ k8s/docker/… │           │ the next begins
   └──────────────────┘           └─────────────┘            │ (resumable)
            ▲                            │ run tests + lint    │
            │  structured error          ▼                     │
   ┌──────────────────┐   fail    ┌──────────────┐            │
   │  retry (≤3)       │◀──────────│   VERIFIER    │────────────┘
   └──────────────────┘   pass→next└──────────────┘
            │
            ▼  exhausted → FAILED → ROLLED_BACK      all steps green → COMPLETED → GitHub PR
```

---

## Requirement map (assessment → where it lives)

| Assessment requirement | Where it's satisfied | Proof |
|---|---|---|
| **Planner** — read-only, strict-JSON plan + dependency hints | `src/orchestrator/planner.py` | emits typed `Step` rows; makes zero write calls |
| **Executor** — separate worker, sandboxed, file-by-file | `src/orchestrator/executor.py`, `src/sandbox/` | `read_file`/`write_file`/`search_replace`, traversal-guarded |
| **Verifier** — test+lint, capture stderr → route back to Executor | `src/orchestrator/verifier.py` | spans show `failed iter=0 → retry → passed iter=1` |
| **Hard-abort + rollback** after retries exhausted | `src/orchestrator/state_machine.py`, `states.py` | `FAILED → ROLLED_BACK`; `make test` covers it |
| **State mgmt & resilience** — crash mid-run, resume without re-planning | `state_machine.py`, `src/storage/` | `tests/test_orchestrator.py::test_crash_recovery_resumes_without_replanning` (Planner called exactly once) |
| **Telemetry** — token per agent node, span durations, loop breakdown | `src/telemetry/tracing.py` | `python3 -m src.main traces --run-id …` |
| **Cost guardrails** — retry + token budget + step timeout | `src/config.py`, `state_machine.py` | §6 below; `test_token_budget_guardrail_trips` |
| **Deployment & horizontal scaling** | `infra/Dockerfile`, `docker-compose.yml`, `k8s/` | §13 below |
| **Tenant isolation** — Agent A can't read Agent B's FS | `src/sandbox/docker_runner.py`, `src/sandbox/k8s_runner.py` | per-run network-isolated container/pod + per-`run_id` workspace |
| **Agent orchestration framework** | `src/orchestrator/graph.py` — LangGraph `StateGraph`, conditional entry resumes mid-flight | [ADR 0001](docs/adr/0001-langgraph-orchestration.md); `tests/test_langgraph_engine.py` runs both engines through identical scenarios |
| **Real-time visibility (push, not polling)** | SSE `GET /runs/{id}/stream` + WebSocket `/runs/{id}/ws`, `src/events.py` | [ADR 0002](docs/adr/0002-push-streaming.md); `tests/test_streaming.py`; §8 below |
| **GitHub PR creation** | `src/git/github.py` — push run branch + open PR on `COMPLETED` | [ADR 0006](docs/adr/0006-github-pr-creation.md); `tests/test_github_pr.py`; §9 below |
| **Dependency-aware planning** | `src/analysis/dep_graph.py` — static import graph + topological step order | [ADR 0005](docs/adr/0005-dependency-graph-planning.md); `tests/test_dep_graph.py` |
| **Patch-based edits (no whole-file rewrites)** | Executor emits ordered, exact, unique search/replace hunks; whole-file only on `create` | [ADR 0003](docs/adr/0003-patch-based-edits.md); `tests/test_executor_patches.py` |
| **Untrusted-code sandboxing (tiered, fail-closed)** | k8s pod-per-run ▸ docker ▸ hardened local; remote repos refuse local | [ADR 0004](docs/adr/0004-sandbox-tiers.md); `tests/test_sandbox_hardening.py`, `tests/test_k8s_sandbox.py` |
| **Integration tests against real services** | `tests/integration/` — Docker, Redis, Postgres, HTTP API, GitHub clone, real LLM providers | `make test-integration` (each test gates on its service) |
| **Retention / cleanup policy** | `src/retention.py` — workspace TTL, record purge, orphan reap | [ADR 0007](docs/adr/0007-retention-policy.md); `tests/test_retention.py`; §12 below |

---

## 1. Install

The **core harness runs on the Python standard library only** (Python ≥ 3.10).
No install is required for the offline demo. Optional extras (API server, real
LLM SDKs, Docker, Langfuse, pytest) live in `requirements.txt`:

```bash
git clone <this-repo> && cd ai-herness-engineering
python3 -m pip install -r requirements.txt pytest   # optional; or: make setup
```

## 2. Run locally (offline, zero API keys)

The default LLM provider is a **deterministic offline `mock`** and the default
sandbox is **`auto`** (Docker if a daemon is reachable, else a local isolated
workspace). So this works immediately:

```bash
make demo
# └─ python3 -m src.main run --repo ./dummy-repos/python-api-sample \
#        --task "Add request validation to the user creation endpoint"
```

You'll see the run go `PENDING → PLANNING → PLAN_READY → EXECUTING_STEP →
VERIFYING_STEP` **fail once** → `RETRYING_STEP` → **fix** → `COMPLETED`, then a
telemetry breakdown, the event log, and the final diff.

Useful commands:

```bash
python3 -m src.main run    --repo <url|path> --task "..."   # create + drive a run
python3 -m src.main resume --run-id run_xxx                 # resume after a crash
python3 -m src.main recover                                 # resume ALL stuck runs
python3 -m src.main status --run-id run_xxx                 # run + step state
python3 -m src.main traces --run-id run_xxx                 # token/latency breakdown
python3 -m src.main log    --run-id run_xxx --follow        # event log (live tail)
python3 -m src.main diff   --run-id run_xxx                 # the produced diff (PR body)
python3 -m src.main pr     --run-id run_xxx                 # push branch + open GitHub PR
python3 -m src.main cleanup --dry-run                       # preview the retention sweep
python3 -m src.main list                                    # all runs
```

## 3. Run against the dummy repos (or any repo)

Two **dependency-free** dummy backends are bundled (their tests/linters use only
stdlib / Node built-ins — nothing to `npm install` or `pip install`):

```bash
make demo            # Python target  (python3 -m unittest + stdlib lint)
make demo-node       # Node target    (node --test     + stdlib lint)

# any public repo (uses a real provider for non-trivial tasks — see §6):
HARNESS_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... \
  python3 -m src.main run --repo https://github.com/you/your-api \
                         --task "Refactor sync DB calls to async"
```

Each dummy ships `app/users.py` (or `src/users.js`) with **no request
validation**; the bundled tests encode the desired behaviour and **fail** until
the harness implements it.

## 4. Architecture — Planner → Executor → Verifier

| Stage | Role | Constraints | Code |
|------|------|-------------|------|
| **Planner** | Scans the tree, builds the **static import-dependency graph** (`src/analysis/dep_graph.py`), reads the snippets ranked most relevant (task-keyword hits + dependents-centrality), emits a **strict-JSON execution plan**. Steps are **topologically sorted** over declared `depends_on` ∪ import edges, so dependencies are edited before dependents ([ADR 0005](docs/adr/0005-dependency-graph-planning.md)). | **Read-only** — only calls sandbox *read* tools; output is validated into typed `Step` rows before anything runs. | `src/orchestrator/planner.py` |
| **Executor** | Applies **one** step as a **patch**: ordered, exact, **unique** search/replace hunks through traversal-guarded file tools; whole-file content is allowed only for `create` ([ADR 0003](docs/adr/0003-patch-based-edits.md)). A patch that fails to apply becomes structured error state and consumes a retry. On a retry it receives the previous failure and must **diagnose + fix**. | Sandboxed; edits via `read_file`/`search_replace` (`write_file` only on `create`). | `src/orchestrator/executor.py` |
| **Verifier** | Runs the step's check commands (tests, linters) **inside the sandbox**; on failure captures stdout/stderr into a structured error blob fed back to the Executor. | Sandboxed; the error blob is the Verifier→Executor contract. | `src/orchestrator/verifier.py` |

The run loop is a **LangGraph `StateGraph`** (`src/orchestrator/graph.py`): one
node per phase, **conditional edges routed on the persisted run status**, and a
conditional entry point so a resumed run enters the graph mid-flight
([ADR 0001](docs/adr/0001-langgraph-orchestration.md)).
`HARNESS_ORCHESTRATOR=auto|langgraph|builtin` selects the engine — `auto` uses
LangGraph when installed and falls back to the dependency-free builtin driver
(`state_machine.py`), which keeps the zero-install offline demo alive. Both
engines wrap the **same stage methods** and the same transition rules, and
`tests/test_langgraph_engine.py` drives both through identical scenarios. Run
states:

```
PENDING → PLANNING → PLAN_READY → EXECUTING_STEP ⇄ VERIFYING_STEP
                                        │               │  fail (budget left)
                                        │               └────────→ RETRYING_STEP ──┐
                                        │  pass → next step  ◀───────────────────────┘
                                        ▼
        COMPLETED              VERIFYING_STEP ── retries exhausted ──→ FAILED → ROLLED_BACK
```

Illegal transitions (e.g. `EXECUTING_STEP → COMPLETED` without verifying) raise,
guarding against silent state corruption (`ALLOWED_TRANSITIONS` in
`src/orchestrator/states.py`). Each **step** also tracks its own status and
iteration count.

## 5. State recovery (resumability)

**Persistence is the queue and the recovery log.** Every transition is committed
to the state store *before* the next begins (autocommit; no half-written
transitions). The store is the single source of truth — never process memory.

If a worker dies at step 4 of 10:

1. The last transition is already persisted (`runs.status`, `runs.current_step`,
   per-step `status`/`iterations`/`last_error`).
2. The workspace is a per-run directory on disk; edits survive.
3. On restart, `Worker.recover()` finds every **non-terminal** run
   (`list_resumable()`) and calls `StateMachine.drive(run_id)` again.
4. `drive()` continues from the persisted state — completed steps are **skipped**
   (`current_step` is past them), and verified steps are **not re-verified**.
5. The **Planner is not re-invoked**: a cached `plan_json` short-circuits
   `PLANNING → PLAN_READY` with no LLM call (cost control on resume).

```bash
# Demonstrate it:
python3 -m src.worker --recover        # resume all stuck runs and exit
python3 -m src.main resume --run-id run_xxx
```

This is covered by `tests/test_orchestrator.py::test_crash_recovery_resumes_without_replanning`,
which crashes mid-verification and asserts the run completes with the Planner
called exactly once.

## 6. Cost guardrails

| Guardrail | Env | Default | Effect |
|-----------|-----|---------|--------|
| Retry budget | `HARNESS_MAX_RETRIES` | `3` | After N failed verify→fix cycles on a step → `FAILED` → `ROLLED_BACK`. |
| Token budget | `HARNESS_MAX_TOKENS_PER_RUN` | `200000` | Token usage is summed across all agent spans; exceeding it fails the run. |
| Step timeout | `HARNESS_STEP_TIMEOUT_SEC` | `600` | Each check command is wall-clock bounded (in-sandbox `timeout`). |
| No re-planning on resume | — | always | Cached plan ⇒ zero extra Planner tokens after a crash. |

Token usage is recorded **per agent node** and folded into the run total by the
tracer; the guardrail trips between steps. The mock provider reports estimated
tokens so the accounting path is exercised offline.

## 7. Sandbox security

Three interchangeable backends implement one `Sandbox` contract
(`src/sandbox/`); the threat model (untrusted **repo code** × untrusted
**model output**) is analyzed in [ADR 0004](docs/adr/0004-sandbox-tiers.md).
**File edits** happen on the run's workspace path; **all untrusted command
execution** (the repo's own tests/lint) is isolated:

* **K8sSandbox** (`HARNESS_SANDBOX=k8s`, `src/sandbox/k8s_runner.py`) — one
  **hardened pod per run**: non-root, `cap_drop=ALL`, read-only rootfs,
  `seccompProfile: RuntimeDefault`, no service-account token, resource
  limits, **deny-all NetworkPolicy** (`infra/k8s/sandbox-networkpolicy.yaml`),
  optional **gVisor** RuntimeClass (`infra/k8s/runtimeclass-gvisor.yaml`).
  Files move over the exec API — no shared volumes, no docker socket anywhere.
* **DockerSandbox** — one container per run with: `--network none` (no network
  for repo code), **read-only root fs** with a single writable **bind mount**
  scoped to *that run's* workspace (no global shared volume → tenant/run
  isolation), `mem_limit` + `pids_limit` + CPU cap, `cap_drop=ALL`, and
  `no-new-privileges`.
* **LocalSandbox** (fallback) — a per-run isolated workspace
  (`workspaces/runs/{run_id}/repo`) with hardened subprocess execution
  (scrubbed env — no host secrets leak in, process-group kill on timeout,
  optional `ulimit`s). Still **no kernel/network isolation** — so it is
  **fail-closed**: remote-URL repos are marked *untrusted* and **refuse this
  backend** unless `HARNESS_ALLOW_LOCAL_UNTRUSTED=1` is set explicitly. It
  remains the auto-fallback only for trusted local paths (CI, Docker-less
  laptops).

Additional hardening that applies to all three: **path-traversal guard** —
every file op is resolved and asserted to stay inside the workspace (a
malicious plan can't touch `/etc/passwd`); `search_replace` refuses ambiguous
(non-unique) matches.

> **Tenant isolation:** runs never share a workspace, volume, or pod; the
> k8s/Docker backends give each run its own network-isolated kernel-level
> boundary. The local backend is *not* a multi-tenant boundary and refuses
> untrusted code by default (above).

Build the sandbox image (enables the Docker backend):

```bash
make sandbox-image        # docker build -t harness-sandbox:latest -f infra/Dockerfile.sandbox .
HARNESS_SANDBOX=docker python3 -m src.main run --repo ./dummy-repos/python-api-sample --task "..."
```

## 8. Real-time visibility (push, not polling)

Every state transition is written to the `events` table **and pushed** to live
subscribers over an event bus — the durable log and the stream are the same
data, so nothing can be seen on the stream that isn't persisted
([ADR 0002](docs/adr/0002-push-streaming.md)):

```bash
curl -N localhost:8000/runs/run_xxx/stream      # SSE: snapshot → live events → end
# wscat -c ws://localhost:8000/runs/run_xxx/ws  # WebSocket: same feed as JSON
python3 -m src.main log --run-id run_xxx --follow   # CLI live tail
```

* **SSE** (`GET /runs/{id}/stream`) sends a snapshot event, then every
  transition as it happens, with heartbeats and an `end` event at terminal
  state. Reconnects are **lossless**: browsers send `Last-Event-ID`
  automatically and the server replays anything missed from the events table.
* **Fan-out** is in-process on a single node and **Redis pub/sub** when the
  API and workers are separate processes (`HARNESS_EVENT_BUS=auto|memory|redis`;
  `auto` follows the queue choice). A dropped pub/sub message can't lose data —
  subscribers catch up from the table (`tests/test_streaming.py`).

## 9. GitHub PR creation

A GitHub-hosted run that reaches `COMPLETED` **pushes its run branch and opens
a pull request** (`src/git/github.py`,
[ADR 0006](docs/adr/0006-github-pr-creation.md)):

* **Token:** `HARNESS_GITHUB_TOKEN` (wins) or `GITHUB_TOKEN`, `repo` scope.
  The push authenticates via ephemeral `GIT_CONFIG_*` env vars — the token
  never appears in argv, stored remotes, or sandbox env.
* **Auto-PR** on completion is default-on when a token is present
  (`HARNESS_AUTO_PR=0` disables). Manual / re-trigger:
  `python3 -m src.main pr --run-id run_xxx` or `POST /runs/{id}/pr` —
  **idempotent**: an existing PR for the branch is returned, not duplicated.
* **Failure containment:** a PR failure emits an `ERROR` event and leaves the
  run `COMPLETED` (the work is not lost); re-trigger any time. GitHub
  Enterprise via `HARNESS_GITHUB_API_URL`.

## 10. Telemetry setup

Spans are **always** persisted to the `telemetry` table — one row per agent
invocation with `input_tokens`, `output_tokens`, `duration_ms`,
`verification_iteration`, and `status` — so you get a full token/latency
breakdown offline with **no external service**:

```bash
python3 -m src.main traces --run-id run_xxx
```

```
  by agent :
     planner   calls=1  tokens=1103   ...
     executor  calls=2  tokens=2205   ...        # note: 2 calls = 1 apply + 1 fix
     verifier  calls=2  tokens=0      178 ms     # verification-loop breakdown
  spans    :
     executor  step-1   ...  apply  iter=0
     verifier  step-1   ...  failed iter=0       # ← the failure
     executor  step-1   ...  retry  iter=1       # ← the self-fix
     verifier  step-1   ...  passed iter=1
```

**Optional Langfuse** visual tracing — set the keys and every span is also
exported as a Langfuse generation (telemetry export never affects run outcome):

```bash
LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... python3 -m src.main run ...
```

## 11. Viewing traces

* **CLI:** `python3 -m src.main traces --run-id run_xxx` (summary + per-span rows)
* **API:** `GET /runs/{id}/traces` returns the same data as JSON
* **Live:** `GET /runs/{id}/stream` (SSE) / `/runs/{id}/ws` (WebSocket) — §8
* **Event log:** `python3 -m src.main log --run-id run_xxx` (every transition)
* **Langfuse:** open your Langfuse project — traces are keyed by `run_id`

## 12. Retention & cleanup

Disk and DB growth are bounded by a **retention sweep** (`src/retention.py`,
[ADR 0007](docs/adr/0007-retention-policy.md)) that every worker runs at
startup and every `HARNESS_SWEEP_INTERVAL_SEC` (default hourly); also on
demand via `python3 -m src.main cleanup [--dry-run]` and as a daily
**k8s CronJob** (`infra/k8s/cleanup-cronjob.yaml`):

| What | Policy | Env |
|---|---|---|
| Workspaces of **terminal** runs | deleted after TTL | `HARNESS_WORKSPACE_TTL_HOURS=72` |
| Old runs + steps + traces + events | purged after the retention window | `HARNESS_RUN_RETENTION_DAYS=30` |
| Orphan workspaces (no run row) | reaped on every sweep | — |

Active (non-terminal) runs are never touched, so a sweep can't break resume.

## 13. Production deployment

`infra/` contains a runnable, production-shaped blueprint:

```
infra/
  Dockerfile            # harness control-plane image (api + worker)
  Dockerfile.sandbox    # the isolated execution image (node + python)
  docker-compose.yml    # api + worker(s) + postgres + redis
  k8s/                  # api, worker (RBAC scoped to pod-per-run sandboxing),
                        # postgres, redis, deny-all sandbox NetworkPolicy,
                        # gVisor RuntimeClass, daily cleanup CronJob
```

```bash
make sandbox-image                       # build the sandbox image first
cd infra && docker compose up -d --build # api:8000, a worker, postgres, redis
docker compose up -d --scale worker=4    # scale the worker pool horizontally
```

**Topology & scaling**

* **API** (stateless) accepts `POST /runs`, persists the run, and enqueues it.
* **Redis** carries run ids; **Postgres** holds run/step/error/telemetry state
  (the source of truth). Set `HARNESS_DATABASE_URL=postgresql://…` — the storage
  layer is dialect-aware (`src/storage/db.py`); SQLite is the zero-setup default.
* **Worker pool** consumes the queue; many workers run in parallel. Each run gets
  an **isolated workspace + network-less sandbox container**; tenants are
  separated by `run_id` (no shared volumes).
* **Crash safety at scale:** every worker's startup `recover()` re-drives any
  non-terminal run, so pod evictions never strand work. Autoscale workers on
  Redis queue depth (KEDA).
* **k8s sandbox (implemented):** workers run `HARNESS_SANDBOX=k8s` — one
  hardened pod per run behind a deny-all NetworkPolicy, optional gVisor
  RuntimeClass, **no docker socket anywhere** (§7,
  [ADR 0004](docs/adr/0004-sandbox-tiers.md)). The worker's RBAC is scoped to
  pod create/exec in its own namespace (`infra/k8s/worker.yaml`).

The API works single-node **without** Redis/Postgres too (SQLite + an in-process
background task): `make api` then
`curl -XPOST :8000/runs -H content-type:application/json \
  -d '{"repo":"./dummy-repos/python-api-sample","task":"Add validation"}'`.

---

## Layout

```
src/
  main.py                       CLI
  config.py                     env-resolved settings + guardrails
  orchestrator/
    graph.py                    LangGraph StateGraph engine        (ADR 0001)
    state_machine.py            stage logic + builtin fallback driver
    states.py                   RunState/StepStatus + transition rules
    planner.py  executor.py  verifier.py
  analysis/dep_graph.py         static import-dependency graph     (ADR 0005)
  sandbox/
    base.py local_runner.py docker_runner.py k8s_runner.py   (ADR 0004)
    file_tools.py             read/search_replace/write (traversal-guarded)
  storage/
    db.py  models.py          SQLite/Postgres state store + repository (DAO)
  events.py                   event bus: in-proc / Redis pub-sub   (ADR 0002)
  retention.py                workspace TTL + record purge sweep   (ADR 0007)
  telemetry/tracing.py        spans + token usage (+ optional Langfuse)
  git/
    repo_manager.py           clone/copy, branch, base-ref, diff, rollback
    github.py                 push + PR creation                   (ADR 0006)
  llm/  client.py prompts.py  anthropic | openai | deterministic mock
  api/server.py  worker.py  queue.py   control plane + worker pool + queue
docs/                          ARCHITECTURE.md + adr/0001…0008
infra/                         Dockerfiles, compose, k8s blueprint
dummy-repos/                   python-api-sample, node-api-sample
tests/                         unit suites + tests/integration (real services)
```

## Configuration

All via env (or a `.env` — see `.env.example`, fully commented). Highlights:
`HARNESS_ORCHESTRATOR` (`auto`/`langgraph`/`builtin`), `HARNESS_LLM_PROVIDER`
(`mock`/`anthropic`/`openai`), `HARNESS_SANDBOX` (`auto`/`docker`/`k8s`/`local`
— fail-closed for untrusted repos), `HARNESS_EVENT_BUS` (`auto`/`memory`/`redis`),
`HARNESS_DATABASE_URL`, `HARNESS_QUEUE` (`db`/`redis`),
`HARNESS_GITHUB_TOKEN`/`HARNESS_AUTO_PR`, `HARNESS_MAX_RETRIES`,
`HARNESS_MAX_TOKENS_PER_RUN`, `HARNESS_WORKSPACE_TTL_HOURS`,
`HARNESS_RUN_RETENTION_DAYS`.

## Testing

```bash
make test                 # unit suites (offline; integration tests auto-skip)
make test-services-up     # redis + postgres on non-default ports for…
make test-integration     # …integration tests: Docker, Redis, Postgres, HTTP
                          #   API, public GitHub clone, real LLM providers
make test-all             # both
```

**Unit** suites cover the happy path (with a real verify-fail→fix cycle),
telemetry breakdown, rollback on exhausted retries, the token guardrail,
**crash recovery without re-planning** (on both engines), patch application,
dependency-graph ordering, sandbox hardening/fail-closed policy, streaming,
PR creation, and retention.

**Integration** suites (`tests/integration/`) exercise the real things — a
real Docker daemon, real Redis queue + pub/sub, real Postgres store, the API
over HTTP, a real public-GitHub clone, and real Anthropic/OpenAI calls — each
test **gates on its service** (skips with a reason when absent, runs in CI/dev
where present) so `make test-all` is safe anywhere.

## Known limitations (honest)

* The **`mock` provider** understands only the two bundled dummy repos (it
  deliberately bugs its first attempt to demo the loop) and is a safe no-op
  elsewhere. Real tasks need `anthropic`/`openai`.
* **Postgres** is dialect-portable and covered by
  `tests/integration/test_postgres_store.py`, but SQLite remains the
  zero-setup default; compose/k8s use Postgres.
* **Docker sandbox** needs a reachable daemon + the built sandbox image. `auto`
  falls back to the hardened local sandbox **only for trusted local paths** —
  remote repos fail closed instead (§7).
* The Planner emits **one step per file** ordered by the import graph; it
  doesn't yet split very large files into sub-edits, and the static analyzer
  resolves Python/JS-style imports only (other languages fall back to declared
  `depends_on`).
* The local sandbox is **not** a kernel-level boundary — multi-tenant
  production should run the k8s (or Docker) backend.
* No web UI; observability is CLI + JSON API + SSE/WebSocket (+ optional
  Langfuse).
