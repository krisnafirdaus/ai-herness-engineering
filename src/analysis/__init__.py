"""Static analysis used by the Planner (import-dependency graph)."""
from .dep_graph import DepGraph, build_dep_graph

__all__ = ["DepGraph", "build_dep_graph"]
