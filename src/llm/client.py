"""LLM client implementations + the deterministic offline ``mock`` provider.

The ``mock`` provider is what makes the harness demoable end-to-end (including a
deliberate verify-fail-then-fix) with no API key. It is NOT a general coding
model: it understands the two bundled dummy repos and falls back to a safe no-op
edit otherwise. Real tasks use ``anthropic`` or ``openai``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..config import settings
from . import prompts


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


# ── Real providers ───────────────────────────────────────────────────────────
class AnthropicClient:
    def __init__(self) -> None:
        import anthropic

        self._c = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.llm_model or "claude-sonnet-4-5"

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResponse:
        msg = self._c.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return LLMResponse(text, msg.usage.input_tokens, msg.usage.output_tokens)


class OpenAIClient:
    def __init__(self) -> None:
        import openai

        self._c = openai.OpenAI(api_key=settings.openai_api_key)
        self.model = settings.llm_model or "gpt-4o"

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResponse:
        resp = self._c.chat.completions.create(
            model=self.model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        u = resp.usage
        return LLMResponse(resp.choices[0].message.content or "",
                           u.prompt_tokens, u.completion_tokens)


# ── Deterministic offline mock ───────────────────────────────────────────────
class MockClient:
    """Reproducible responses driven by the embedded ```json:context block."""

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> LLMResponse:
        ctx = prompts.extract_context(user) or {}
        role = ctx.get("role")
        if role == "planner":
            text = self._plan(ctx)
        elif role == "executor":
            text = self._execute(ctx)
        else:
            text = "{}"
        return LLMResponse(text, _est_tokens(user), _est_tokens(text))

    # -- planner --
    def _plan(self, ctx: dict) -> str:
        task = ctx.get("task", "")
        tree: list[str] = ctx.get("tree", [])
        checks = self._infer_checks(tree)
        target = self._pick_target(task, tree)
        plan = {
            "task": task,
            "steps": [{
                "id": "step-1",
                "file": target,
                "action": "modify",
                "reason": f"Implement the task in the primary source file ({target}).",
                "checks": checks,
            }],
        }
        return json.dumps(plan, indent=2)

    @staticmethod
    def _infer_checks(tree: list[str]) -> list[str]:
        s = set(tree)
        if "scripts/lint.py" in s or any(p.startswith("tests/") for p in tree):
            return ["python3 -m unittest discover -s tests -t .", "python3 scripts/lint.py"]
        if "package.json" in s:
            return ["node --test", "node scripts/lint.js"]
        return ["python3 -m unittest discover", "python3 -m py_compile $(git ls-files '*.py')"]

    @staticmethod
    def _pick_target(task: str, tree: list[str]) -> str:
        tokens = [t for t in re.split(r"\W+", task.lower()) if len(t) > 2]
        sources = [p for p in tree if p.endswith((".py", ".js", ".ts"))
                   and not re.search(r"(^|/)(tests?|scripts|node_modules)/", p)]
        if not sources:
            sources = tree or ["app/main.py"]

        def score(path: str) -> tuple:
            low = path.lower()
            hits = sum(1 for t in tokens if t in low)
            in_src = 1 if re.search(r"(^|/)(app|src)/", low) else 0
            return (hits, in_src, -len(path))

        return max(sources, key=score)

    # -- executor (patch-based, mirrors the real EXECUTOR_SYSTEM contract) ----
    def _execute(self, ctx: dict) -> str:
        step = ctx.get("step", {})
        file = step.get("file", "")
        current = ctx.get("current_content", "") or ""
        base = file.rsplit("/", 1)[-1]

        if base == "users.py":
            edits, summary = self._py_users_edits(current)
        elif base == "users.js":
            edits, summary = self._js_users_edits(current)
        else:
            # Unknown file: an empty patch is a safe no-op so arbitrary repos
            # don't crash the mock. (Real edits require a real provider.)
            edits, summary = [], "No-op patch (mock has no recipe for this file)"

        return json.dumps({"file": file, "action": "modify",
                           "edits": edits, "summary": summary})

    @staticmethod
    def _py_users_edits(current: str) -> tuple[list[dict], str]:
        # Retry: the buggy call is present — patch just the bad name.
        if "validate_user(payload)" in current:
            return ([{"find": "    validate_user(payload)",
                      "replace": "    validate_payload(payload)"}],
                    "Fix NameError: call validate_payload, the function that "
                    "actually exists")
        # First attempt: insert validation, but call it by the WRONG name so the
        # verify -> retry -> fix loop is exercised (NameError at runtime).
        if "validate_payload" not in current:
            return ([
                {"find": "_USERS = {}",
                 "replace": "_USERS = {}\n\n" + _PY_VALIDATE_FN.rstrip("\n")},
                {"find": "def create_user(payload):\n    user_id",
                 "replace": "def create_user(payload):\n"
                            "    validate_user(payload)\n    user_id"},
            ], "Add request validation before persisting the user")
        return [], "No-op patch (validation already present)"

    @staticmethod
    def _js_users_edits(current: str) -> tuple[list[dict], str]:
        if "validateUser(payload)" in current:
            return ([{"find": "  validateUser(payload);",
                      "replace": "  validatePayload(payload);"}],
                    "Fix ReferenceError: call validatePayload")
        if "validatePayload" not in current:
            return ([
                {"find": "const _users = new Map();",
                 "replace": "const _users = new Map();\n\n" + _JS_VALIDATE_FN.rstrip("\n")},
                {"find": "function createUser(payload) {\n  const id",
                 "replace": "function createUser(payload) {\n"
                            "  validateUser(payload);\n  const id"},
            ], "Add request validation before persisting the user")
        return [], "No-op patch (validation already present)"


# ── Mock patch snippets (inserted blocks) ────────────────────────────────────
_PY_VALIDATE_FN = '''\
REQUIRED_FIELDS = ("email", "name")


def validate_payload(payload):
    """Reject malformed user-creation requests before they are persisted."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    for field in REQUIRED_FIELDS:
        if not payload.get(field):
            raise ValueError(f"missing required field: {field}")
    if "@" not in payload["email"]:
        raise ValueError("invalid email")
'''

_JS_VALIDATE_FN = '''\
const REQUIRED_FIELDS = ["email", "name"];

function validatePayload(payload) {
  if (typeof payload !== "object" || payload === null) {
    throw new Error("payload must be an object");
  }
  for (const field of REQUIRED_FIELDS) {
    if (!payload[field]) {
      throw new Error(`missing required field: ${field}`);
    }
  }
  if (!String(payload.email).includes("@")) {
    throw new Error("invalid email");
  }
}
'''


# ── Factory ──────────────────────────────────────────────────────────────────
def get_client():
    provider = settings.llm_provider
    if provider == "mock":
        return MockClient()
    if provider == "anthropic":
        return AnthropicClient()
    if provider == "openai":
        return OpenAIClient()
    raise ValueError(f"unknown HARNESS_LLM_PROVIDER: {provider!r}")
