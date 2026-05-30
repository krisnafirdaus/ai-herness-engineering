# AI Agent Harness — Autonomous Multi-File Refactoring Runner

A **production-shaped harness** that takes a public GitHub repo + a task, then
drives three agents — **Planner → Executor → Verifier** — to plan a refactor,
edit files in an isolated sandbox, run the repo's tests/linters, **fix its own
failures**, and stop with a **rollback** after 3 failed attempts. Every run is
backed by a **persistent, resumable state machine**, so a worker crash at step 4
of 10 resumes from step 4 — without re-planning or re-paying for verified steps.

> The emphasis is the *system*: orchestration, safety, resumability, sandboxing,
> the verification loop, telemetry, and a deployment blueprint — not the prompt.

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
   ┌──────────────────┐   edits    ┌─────────────┐            │ every transition
   │   EXECUTOR        │ ─────────▶ │   SANDBOX    │           │ persisted before
   │  (one step)       │           │ docker/local │           │ the next begins
   └──────────────────┘           └─────────────┘            │ (resumable)
            ▲                            │ run tests + lint    │
            │  structured error          ▼                     │
   ┌──────────────────┐   fail    ┌──────────────┐            │
   │  retry (≤3)       │◀──────────│   VERIFIER    │────────────┘
   └──────────────────┘   pass→next└──────────────┘
            │
            ▼  exhausted → FAILED → ROLLED_BACK      all steps green → COMPLETED (PR-ready)
```

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
python3 -m src.main log    --run-id run_xxx                 # event log
python3 -m src.main diff   --run-id run_xxx                 # the produced diff (PR body)
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
| **Planner** | Scans the tree, reads key snippets, builds dependency hints, emits a **strict-JSON execution plan** (one file per step + the checks to run). | **Read-only** — only calls sandbox *read* tools; output is validated into typed `Step` rows before anything runs. | `src/orchestrator/planner.py` |
| **Executor** | Applies **one** step by emitting the full new file content, written through traversal-guarded file tools. On a retry it receives the previous failure and must **diagnose + fix**. | Sandboxed; edits via `read_file`/`write_file`/`search_replace` only. | `src/orchestrator/executor.py` |
| **Verifier** | Runs the step's check commands (tests, linters) **inside the sandbox**; on failure captures stdout/stderr into a structured error blob fed back to the Executor. | Sandboxed; the error blob is the Verifier→Executor contract. | `src/orchestrator/verifier.py` |

The **state machine** (`src/orchestrator/state_machine.py`) is the single driver
and the centerpiece. Run states:

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

Two interchangeable backends implement one `Sandbox` contract
(`src/sandbox/`). **File edits** happen on the run's workspace path; **all
untrusted command execution** (the repo's own tests/lint) is isolated:

* **DockerSandbox** (primary) — one container per run with:
  `--network none` (no network for repo code), **read-only root fs** with a
  single writable **bind mount** scoped to *that run's* workspace (no global
  shared volume → tenant/run isolation), `mem_limit` + `pids_limit` + CPU cap,
  `cap_drop=ALL`, and `no-new-privileges`.
* **LocalSandbox** (fallback) — a per-run isolated workspace
  (`workspaces/runs/{run_id}/repo`) with subprocess execution. Honestly weaker:
  **no kernel/network isolation**; it exists so the harness always runs (CI,
  Docker-less laptops) and is the auto-fallback when no daemon is present.

Additional hardening that applies to both: **path-traversal guard** — every file
op is resolved and asserted to stay inside the workspace (a malicious plan can't
touch `/etc/passwd`); `search_replace` refuses ambiguous (non-unique) matches.

> **Tenant isolation:** runs never share a workspace or volume; the Docker
> backend gives each its own network-less container. The local backend is *not*
> a multi-tenant boundary — for untrusted, multi-tenant production use the Docker
> backend (or the k8s gVisor/Job pattern noted in `infra/k8s/worker.yaml`).

Build the sandbox image (enables the Docker backend):

```bash
make sandbox-image        # docker build -t harness-sandbox:latest -f infra/Dockerfile.sandbox .
HARNESS_SANDBOX=docker python3 -m src.main run --repo ./dummy-repos/python-api-sample --task "..."
```

## 8. Telemetry setup

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

## 9. Viewing traces

* **CLI:** `python3 -m src.main traces --run-id run_xxx` (summary + per-span rows)
* **API:** `GET /runs/{id}/traces` returns the same data as JSON
* **Event log:** `python3 -m src.main log --run-id run_xxx` (every transition)
* **Langfuse:** open your Langfuse project — traces are keyed by `run_id`

## 10. Production deployment

`infra/` contains a runnable, production-shaped blueprint:

```
infra/
  Dockerfile            # harness control-plane image (api + worker)
  Dockerfile.sandbox    # the isolated execution image (node + python)
  docker-compose.yml    # api + worker(s) + postgres + redis
  k8s/{api,worker,postgres,redis}.yaml
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
* **k8s note:** don't mount the host docker socket in a multi-tenant cluster —
  use a gVisor/Kata RuntimeClass or a per-run Job with a deny-all NetworkPolicy
  (a drop-in `Sandbox` backend); see `infra/k8s/worker.yaml`.

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
    state_machine.py            the resumable driver (centerpiece)
    states.py                   RunState/StepStatus + transition rules
    planner.py  executor.py  verifier.py
  sandbox/
    base.py local_runner.py docker_runner.py   isolation backends
    file_tools.py             read/write/search_replace (traversal-guarded)
  storage/
    db.py  models.py          SQLite/Postgres state store + repository (DAO)
  telemetry/tracing.py        spans + token usage (+ optional Langfuse)
  git/repo_manager.py         clone/copy, branch, base-ref, diff, rollback
  llm/  client.py prompts.py  anthropic | openai | deterministic mock
  api/server.py  worker.py  queue.py   control plane + worker pool + queue
infra/                         Dockerfiles, compose, k8s blueprint
dummy-repos/                   python-api-sample, node-api-sample
tests/                         orchestrator + unit tests
```

## Configuration

All via env (or a `.env` — see `.env.example`). Highlights: `HARNESS_LLM_PROVIDER`
(`mock`/`anthropic`/`openai`), `HARNESS_SANDBOX` (`auto`/`docker`/`local`),
`HARNESS_DATABASE_URL`, `HARNESS_QUEUE` (`db`/`redis`), `HARNESS_MAX_RETRIES`,
`HARNESS_MAX_TOKENS_PER_RUN`.

## Testing

```bash
make test        # python3 -m pytest -q
```

Covers the happy path (with a real verify-fail→fix cycle), telemetry breakdown,
rollback on exhausted retries, the token guardrail, **crash recovery without
re-planning**, path-traversal blocking, and the state-transition guard.

## Known limitations (honest)

* The **`mock` provider** understands only the two bundled dummy repos (it
  deliberately bugs its first attempt to demo the loop) and is a safe no-op
  elsewhere. Real tasks need `anthropic`/`openai`.
* **Postgres** is wired and dialect-portable but **SQLite is the verified path**
  in this submission (no PG daemon in the build env); compose/k8s use Postgres.
* **Docker sandbox** requires a reachable daemon + the built sandbox image; with
  neither, `auto` transparently falls back to the (weaker) local sandbox.
* The Planner emits **one step per file**; it doesn't yet split very large files
  into sub-edits or model cross-file ordering beyond simple dependency hints.
* The local sandbox is **not** a security boundary for untrusted multi-tenant
  code — use the Docker/k8s backends for that.
* No web UI; observability is CLI + JSON API (+ optional Langfuse).
