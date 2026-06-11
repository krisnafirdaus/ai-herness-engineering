"""Static import-dependency graph over a repository tree.

The Planner uses this to reason about edit ordering and blast radius instead
of guessing from file names:

* **Python** files are parsed with :mod:`ast`; absolute and relative imports
  are resolved against the repo tree (``pkg/mod.py``, ``pkg/mod/__init__.py``).
* **JS/TS** files are scanned for ``import ... from '...'`` / ``require('...')``
  and relative specifiers are resolved with the usual extension/index probing.

Only intra-repository edges are kept — external packages are not part of the
graph. The graph exposes forward edges (``imports_of``), reverse edges
(``dependents_of`` — the impact set of changing a file), cycle detection, and
a stable topological ordering used to schedule plan steps dependencies-first.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

_PY_EXT = (".py",)
_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_JS_RESOLVE_SUFFIXES = ("", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
                        "/index.js", "/index.ts", "/index.jsx", "/index.tsx")
_MAX_PARSE_BYTES = 400_000  # skip pathological/generated files

_JS_IMPORT_RE = re.compile(
    r"""(?:^|\s)import(?:\s+[\w*{},\s$]+?\s+from)?\s*['"]([^'"]+)['"]"""
    r"""|require\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|import\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)


@dataclass
class DepGraph:
    """Forward + reverse intra-repo import edges, keyed by repo-relative path."""

    imports: dict[str, set[str]] = field(default_factory=dict)
    dependents: dict[str, set[str]] = field(default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)

    def imports_of(self, path: str) -> set[str]:
        return self.imports.get(path, set())

    def dependents_of(self, path: str) -> set[str]:
        """Files that import ``path`` — the immediate impact set of editing it."""
        return self.dependents.get(path, set())

    def transitive_dependents(self, path: str, limit: int = 50) -> set[str]:
        """Everything that may break if ``path`` changes (BFS over reverse edges)."""
        seen: set[str] = set()
        frontier = [path]
        while frontier and len(seen) < limit:
            cur = frontier.pop()
            for dep in self.dependents.get(cur, ()):
                if dep not in seen:
                    seen.add(dep)
                    frontier.append(dep)
        return seen

    def cycles(self) -> list[list[str]]:
        """Import cycles (each reported once as a path list)."""
        out: list[list[str]] = []
        color: dict[str, int] = {}  # 0 unvisited / 1 on-stack / 2 done
        stack: list[str] = []

        def visit(node: str) -> None:
            color[node] = 1
            stack.append(node)
            for nxt in sorted(self.imports.get(node, ())):
                c = color.get(nxt, 0)
                if c == 0:
                    visit(nxt)
                elif c == 1:
                    out.append(stack[stack.index(nxt):] + [nxt])
            stack.pop()
            color[node] = 2

        for n in sorted(self.imports):
            if color.get(n, 0) == 0:
                visit(n)
        return out

    def topo_order(self, files: list[str]) -> list[str]:
        """Order ``files`` so dependencies come before dependents.

        Only edges between the given files matter. Stable: ties keep the input
        order. Cycles are tolerated — members of a cycle stay in input order
        rather than failing the plan.
        """
        index = {f: i for i, f in enumerate(files)}
        subset = set(files)
        # edge dep -> dependent, restricted to the subset
        out_edges: dict[str, set[str]] = {f: set() for f in files}
        indeg: dict[str, int] = {f: 0 for f in files}
        for f in files:
            for dep in self.imports.get(f, ()):
                if dep in subset and dep != f:
                    if f not in out_edges[dep]:
                        out_edges[dep].add(f)
                        indeg[f] += 1

        ready = sorted((f for f in files if indeg[f] == 0), key=index.__getitem__)
        order: list[str] = []
        while ready:
            cur = ready.pop(0)
            order.append(cur)
            for nxt in sorted(out_edges[cur], key=index.__getitem__):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
            ready.sort(key=index.__getitem__)
        # Cycle remainder: preserve input order.
        if len(order) < len(files):
            order.extend(sorted((f for f in files if f not in set(order)),
                                key=index.__getitem__))
        return order

    def summary_for(self, paths: list[str], *, max_edges: int = 12) -> dict:
        """Compact per-file dependency context for the Planner prompt."""
        info = {}
        for p in paths:
            info[p] = {
                "imports": sorted(self.imports_of(p))[:max_edges],
                "imported_by": sorted(self.dependents_of(p))[:max_edges],
            }
        return {
            "files": info,
            "cycles": [c for c in self.cycles()[:5]],
            "note": ("'imported_by' is the blast radius of editing that file; "
                     "edit dependencies before dependents"),
        }


def build_dep_graph(tree: list[str], read_file) -> DepGraph:
    """Parse every source file in ``tree`` and link intra-repo imports.

    ``read_file(path) -> str`` is injected (the Planner passes the sandbox's
    read-only file accessor, keeping this module side-effect free).
    """
    tree_set = set(tree)
    graph = DepGraph()
    for path in tree:
        if path.endswith(_PY_EXT):
            targets = _python_imports(path, tree_set, read_file, graph)
        elif path.endswith(_JS_EXT):
            targets = _js_imports(path, tree_set, read_file, graph)
        else:
            continue
        if targets:
            graph.imports.setdefault(path, set()).update(targets)
            for t in targets:
                graph.dependents.setdefault(t, set()).add(path)
    return graph


# ── Python resolution ─────────────────────────────────────────────────────────
def _python_imports(path: str, tree: set[str], read_file, graph: DepGraph) -> set[str]:
    src = _read_bounded(path, read_file, graph)
    if src is None:
        return set()
    try:
        mod = ast.parse(src)
    except SyntaxError as e:
        graph.parse_errors.append(f"{path}: {e.msg}")
        return set()

    pkg_parts = path.split("/")[:-1]  # directory of the importing file
    found: set[str] = set()
    for node in ast.walk(mod):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found |= _resolve_py_module(alias.name.split("."), tree)
        elif isinstance(node, ast.ImportFrom):
            base = list(pkg_parts)
            if node.level:  # relative import: level 1 = current package
                base = pkg_parts[:len(pkg_parts) - (node.level - 1)] \
                    if node.level <= len(pkg_parts) + 1 else None
                if base is None:
                    continue
            module_parts = node.module.split(".") if node.module else []
            prefix = (base if node.level else []) + module_parts
            hit = _resolve_py_module(prefix, tree)
            if hit:
                found |= hit
            # `from pkg import name` where name is itself a module
            for alias in node.names:
                found |= _resolve_py_module(prefix + [alias.name], tree)
    found.discard(path)
    return found


def _resolve_py_module(parts: list[str], tree: set[str]) -> set[str]:
    if not parts:
        return set()
    stem = "/".join(p for p in parts if p)
    for cand in (f"{stem}.py", f"{stem}/__init__.py"):
        if cand in tree:
            return {cand}
    return set()


# ── JS/TS resolution ──────────────────────────────────────────────────────────
def _js_imports(path: str, tree: set[str], read_file, graph: DepGraph) -> set[str]:
    src = _read_bounded(path, read_file, graph)
    if src is None:
        return set()
    found: set[str] = set()
    base_dir = path.rsplit("/", 1)[0] if "/" in path else ""
    for m in _JS_IMPORT_RE.finditer(src):
        spec = next(g for g in m.groups() if g)
        if not spec.startswith("."):
            continue  # external package
        target = _normalize("/".join(filter(None, [base_dir, spec])))
        for suffix in _JS_RESOLVE_SUFFIXES:
            cand = target + suffix
            if cand in tree:
                found.add(cand)
                break
    found.discard(path)
    return found


def _normalize(p: str) -> str:
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _read_bounded(path: str, read_file, graph: DepGraph) -> str | None:
    try:
        src = read_file(path)
    except Exception as e:  # unreadable file: record, keep building
        graph.parse_errors.append(f"{path}: {e}")
        return None
    if len(src) > _MAX_PARSE_BYTES:
        graph.parse_errors.append(f"{path}: skipped ({len(src)} bytes)")
        return None
    return src
