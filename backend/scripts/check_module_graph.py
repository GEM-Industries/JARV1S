#!/usr/bin/env python3
"""
Module-graph enforcement for the JARV1S backend.

Protects the work in docs/proposals/MODULE_GRAPH_CLEANUP.md from silently
regressing by forbidding import cycles **between top-level packages**:
``core``, ``services``, ``plugins``, ``api``. The current direction of
allowed imports is::

    api -> core, services, plugins
    core -> services, plugins
    plugins -> core, services
    services -> core

Pre-existing submodule cycles inside ``core`` (e.g. ``core.prompts`` <->
``core.turns``) are tracked in docs/analysis/module-graph-before.svg but
are out of scope for the proposal that introduced this check; they can be
added to this file once those seams are straightened.

Exits non-zero on cycle detection. Fast enough for a pre-commit hook.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import grimp

TOP_LEVEL_PACKAGES = ("core", "services", "plugins", "api")


def _find_cycles_among(graph: grimp.Graph, modules: list[str]) -> list[list[str]]:
    """Return all simple cycles among the given modules using direct imports."""
    module_set = set(modules)
    adj: dict[str, set[str]] = {m: set() for m in module_set}
    for src in modules:
        for dst in graph.find_modules_directly_imported_by(src):
            if dst in module_set and dst != src:
                adj[src].add(dst)

    cycles: list[list[str]] = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {m: WHITE for m in module_set}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nbr in sorted(adj[node]):
            if color[nbr] == GRAY:
                idx = stack.index(nbr)
                cycles.append(stack[idx:] + [nbr])
            elif color[nbr] == WHITE:
                dfs(nbr)
        stack.pop()
        color[node] = BLACK

    for m in sorted(module_set):
        if color[m] == WHITE:
            dfs(m)
    return cycles


def main() -> int:
    backend_root = Path(__file__).resolve().parents[1]
    os.chdir(backend_root)

    graph = grimp.build_graph(*TOP_LEVEL_PACKAGES)
    pkg_cycles = _find_cycles_among(graph, list(TOP_LEVEL_PACKAGES))

    if not pkg_cycles:
        print(f"module-graph: OK (no cycles across {', '.join(TOP_LEVEL_PACKAGES)})")
        return 0

    print("module-graph: top-level package cycles detected:")
    for cycle in pkg_cycles:
        print("  " + " -> ".join(cycle))
    return 1


if __name__ == "__main__":
    sys.exit(main())
