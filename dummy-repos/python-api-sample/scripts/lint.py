#!/usr/bin/env python3
"""Tiny stdlib-only linter so the dummy repo's `lint` check needs no installs.

Checks every tracked .py file under app/, tests/ and scripts/ for:
  * syntax errors (py_compile)
  * trailing whitespace
  * literal tab indentation
  * lines longer than 120 characters
Exits non-zero (with a report) on the first category of violations found.
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRS = ["app", "tests", "scripts"]
MAX_LEN = 120


def main() -> int:
    violations: list[str] = []
    for d in DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            try:
                py_compile.compile(str(path), doraise=True)
            except py_compile.PyCompileError as e:
                violations.append(f"{path}: syntax error: {e.msg.strip()}")
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                rel = path.relative_to(ROOT)
                if line.rstrip("\n") != line.rstrip():
                    violations.append(f"{rel}:{n}: trailing whitespace")
                if "\t" in line:
                    violations.append(f"{rel}:{n}: tab character")
                if len(line) > MAX_LEN:
                    violations.append(f"{rel}:{n}: line too long ({len(line)} > {MAX_LEN})")

    if violations:
        print("LINT FAILED:")
        for v in violations:
            print("  " + v)
        return 1
    print("lint ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
