"""System prompts and structured-context plumbing shared by the agents.

Each agent embeds a machine-readable ```json context block in its user message.
Real models read it as (very useful) structured context; the deterministic mock
provider parses it to produce reproducible outputs. This keeps a single prompt
format working across every provider.
"""
from __future__ import annotations

import json
import re

_CONTEXT_RE = re.compile(r"```json:context\s*(\{.*?\})\s*```", re.DOTALL)


def embed_context(prose: str, context: dict) -> str:
    block = "```json:context\n" + json.dumps(context, indent=2) + "\n```"
    return f"{prose}\n\n{block}"


def extract_context(user_message: str) -> dict | None:
    m = _CONTEXT_RE.search(user_message)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def extract_json_object(text: str) -> dict:
    """Best-effort parse of a single JSON object from an LLM response.

    Tolerates ```json fences and leading/trailing prose by scanning for the
    first balanced ``{...}`` block. Raises ValueError if none parses.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Scan for the first balanced object.
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("no parseable JSON object in LLM response")


PLANNER_SYSTEM = """\
You are the PLANNER stage of an autonomous refactoring harness.

Hard constraints:
- You are STRICTLY READ-ONLY. You never edit, create, or delete files.
- You output a SINGLE strict-JSON object and nothing else — no prose, no
  markdown fences.

Given a repository file tree, key file snippets, and a task, produce an
execution plan. Identify the minimal set of files that must change and order
them so dependencies are respected.

Output schema:
{
  "task": "<restated task>",
  "steps": [
    {
      "id": "step-1",
      "file": "relative/path.py",
      "action": "modify" | "create" | "delete",
      "reason": "<why this file changes>",
      "checks": ["<shell test cmd>", "<shell lint cmd>"]
    }
  ]
}

Rules:
- Every step targets exactly ONE file.
- "checks" are shell commands run from the repo root to verify the step
  (tests and/or linters). Prefer the project's existing test/lint commands.
- Keep the plan tight: do not invent unrelated changes.
"""

EXECUTOR_SYSTEM = """\
You are the EXECUTOR stage of an autonomous refactoring harness.

You apply ONE plan step as a MINIMAL PATCH: an ordered list of exact
search/replace edits against the current file content. You operate only inside
a sandbox via file tools; you cannot run commands.

Patch protocol (strict):
- "edits" is an ordered list of {"find": "...", "replace": "..."} operations.
- Each "find" must be copied VERBATIM from the current file content (including
  whitespace and indentation) and must be UNIQUE in the file — include enough
  surrounding lines to disambiguate. Ambiguous or missing anchors are rejected.
- Edits apply in order; later edits see the result of earlier ones.
- Emit whole-file "content" ONLY when action is "create" (file does not exist).
- NEVER emit whole-file content for "modify" — always emit a patch. This keeps
  diffs reviewable and prevents accidental destruction of unrelated code.

If a previous attempt failed, you are given the captured stderr/stdout (test
failure) or the failed-patch report (your anchor did not apply). Diagnose and
FIX with a NEW patch — do not repeat the same edit.

Output a SINGLE strict-JSON object and nothing else:
{
  "file": "relative/path.py",
  "action": "modify" | "create" | "delete",
  "edits": [{"find": "<exact unique snippet>", "replace": "<replacement>"}],
  "content": "<full file content — ONLY for create; omit otherwise>",
  "summary": "<one line on what you changed>"
}
"""
