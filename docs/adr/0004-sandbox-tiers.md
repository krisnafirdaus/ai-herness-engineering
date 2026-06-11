# ADR 0004 — Sandbox tiers and the fail-closed trust policy

**Status:** accepted

## Threat model

The harness executes two kinds of untrusted input: (a) **repo code** — the
target repository's tests/linters run arbitrary commands; (b) **model
output** — edits are applied to the workspace. Edits are already confined by
the traversal-guarded file tools; this ADR is about (a).

## Decision: three backends, one contract, explicit trust

| Backend | Boundary | Use |
|---|---|---|
| `K8sSandbox` | pod-per-run: non-root, caps dropped, read-only rootfs, no SA token, seccomp, resource limits, **deny-all NetworkPolicy**, optional **gVisor** RuntimeClass | multi-tenant production (cluster) |
| `DockerSandbox` | container-per-run: `--network none`, read-only rootfs, per-run bind mount, mem/pids/cpu caps, `cap_drop=ALL`, `no-new-privileges` | single-host production / dev |
| `LocalSandbox` | subprocess with scrubbed env allowlist (no harness secrets), own process group + group-kill on timeout, `ulimit` caps | **trusted code only**: CI, the offline demo, the operator's own repos |

**Fail closed:** `select_sandbox(trusted=...)` refuses to put a repo cloned
from a **remote URL** into `LocalSandbox`. The old behavior — `auto` silently
degrading to the weakest backend when Docker was missing — was a security
posture bug: the security level depended on what happened to be installed.
Now the run errors with an actionable message unless the operator explicitly
sets `HARNESS_ALLOW_LOCAL_UNTRUSTED=1`. Local-path repos (the operator's own
checkout) remain allowed.

**Secrets:** the local backend passes an environment allowlist (`PATH`,
`HOME`, locale, tmpdir) — `ANTHROPIC_API_KEY`/`GITHUB_TOKEN`/`HARNESS_*`
never reach repo code. Docker/k8s never forwarded them. The k8s pod
additionally has no service-account token and no service links.

**Network isolation is enforced outside the sandbox** (daemon flag /
NetworkPolicy), so sandboxed code can't turn it off from inside.

## Consequences

* "Which isolation level am I getting?" has a deterministic, configured
  answer, never an accidental one.
* The k8s blueprint is implemented code with manifests + RBAC, not prose.
