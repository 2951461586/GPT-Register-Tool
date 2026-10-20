"""Generate the dependency graph that ``docs/current/dependency-graph.md`` shows.

Why this exists
---------------
Every review graph this project has used (pi-lens and its predecessors) is a
**snapshot**: it is rebuilt by hand, drifts from the source within a day, and
reports at directory granularity, which collapses ``sms_tool/`` into one node
and therefore reports "one cycle spanning every subpackage". The 2026-10-04
scan had to decompose that by hand
(``docs/audits/scans/scan-2026-10-04-import-cycle-decomposition.md``), and the
2026-10-21 plan found the same artefact again.

``scripts/import_layer_ratchet.py`` already resolves imports exactly, but it
only keeps **cross-directory** edges -- enough to ratchet, not enough to see a
cycle *inside* one directory (the nine-module registration stage cluster is
exactly that). This script adds the missing view and, more importantly, makes it
a **generated artefact**: the numbers live in a file CI regenerates and
compares, so they cannot go stale.

What it measures
----------------
Module-level import edges between ``sms_tool`` modules, resolved through
absolute and relative imports (aliases included, so ``from . import x`` counts
when ``x`` is a module). Function-local imports are counted separately: this
repo uses them deliberately to break runtime cycles, so folding them into the
cycle view would report a fix as a defect.

The two resolvers must agree. ``tests/governance/test_dependency_graph.py``
pins this script's cross-directory counts against ``import_layer_ratchet``, so
a divergence fails instead of silently producing a second truth.

Usage:
    python scripts/dependency_graph.py            # check the committed doc
    python scripts/dependency_graph.py --write     # regenerate it
    python scripts/dependency_graph.py --detail    # print the edge list
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "sms_tool"
DOC = ROOT / "docs" / "current" / "dependency-graph.md"
EXCLUDED_DIRS = {"__pycache__", "_vendor"}

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The directory vocabulary is shared with the ratchet on purpose: two different
# notions of "which directory is this" would produce two different edge counts.
from import_layer_ratchet import SUBPACKAGES  # noqa: E402  # type: ignore


@dataclass(frozen=True)
class ModuleGraph:
    """Resolved module-level edges, plus the delayed ones kept apart."""

    modules: dict[str, Path]
    is_package: dict[str, bool]
    edges: dict[str, set[str]]
    delayed: dict[str, set[str]]

    def module_edge_count(self) -> int:
        return sum(len(targets) for targets in self.edges.values())

    def delayed_edge_count(self) -> int:
        return sum(len(targets) for targets in self.delayed.values())


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("").as_posix()
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _index(root: Path) -> tuple[list[Path], dict[str, Path], dict[str, bool]]:
    files = [path for path in sorted((root / PACKAGE).rglob("*.py")) if not (EXCLUDED_DIRS & set(path.parts))]
    modules = {_module_name(root, path): path for path in files}
    is_package = {name: path.name == "__init__.py" for name, path in modules.items()}
    return files, modules, is_package


def _resolve(spec: str, modules: dict[str, Path]) -> str | None:
    parts = spec.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in modules:
            return candidate
    return None


def _relative_base(module: str, is_package: bool, level: int) -> str | None:
    parts = module.split(".")
    if not is_package:
        # ``from .x import y`` in ``sms_tool/a/b.py`` means ``sms_tool/a``: the
        # module's *parent* package, not the module itself.
        parts = parts[:-1]
    keep = len(parts) - (level - 1)
    if keep <= 0:
        return None
    return ".".join(parts[:keep])


def _import_targets(node: ast.AST, module: str, is_package: bool, own_package: str) -> tuple[str | None, list[str]]:
    """``(head, aliases)`` for one import statement.

    ``head`` is the module named before ``import``; aliases are the names after
    it. Both are kept: a package whose ``__init__`` re-exports submodules while
    those submodules import the package *is* a circular import, and hiding it
    would report the facade pattern as healthy.
    """
    if isinstance(node, ast.Import):
        return None, [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return None, []
    if node.level:
        base = _relative_base(module, is_package, node.level)
        if base is None:
            return None, []
        named = base + ("." + node.module if node.module else "")
    else:
        named = node.module or ""
    aliases = [f"{named}.{alias.name}" if named else alias.name for alias in node.names if alias.name != "*"]
    if not aliases and named == own_package:
        # ``from . import *`` names no module of its own; the package edge would
        # only restate "I am in this package".
        return None, []
    return named or None, aliases


def build(root: Path | None = None) -> ModuleGraph:
    """Resolve the module graph for ``root/sms_tool``."""
    root = ROOT if root is None else root
    files, modules, is_package = _index(root)
    edges: dict[str, set[str]] = defaultdict(set)
    delayed: dict[str, set[str]] = defaultdict(set)

    for path in files:
        module = _module_name(root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        nested: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        nested.add(id(child))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            bucket = delayed if id(node) in nested else edges
            own_package = module if is_package[module] else module.rsplit(".", 1)[0]
            head, aliases = _import_targets(node, module, is_package[module], own_package)
            targets = {
                resolved for resolved in (_resolve(spec, modules) for spec in [head, *aliases] if spec) if resolved
            }
            for target in targets:
                if target != module:
                    bucket[module].add(target)

    return ModuleGraph(
        modules=modules,
        is_package=is_package,
        edges={key: value for key, value in edges.items()},
        delayed={key: value for key, value in delayed.items()},
    )


def directory_of(module: str) -> str:
    parts = module.split(".")
    if len(parts) >= 2 and parts[1] in SUBPACKAGES:
        return f"{PACKAGE}/{parts[1]}"
    return PACKAGE


def strongly_connected(graph: ModuleGraph) -> list[list[str]]:
    """Tarjan, iterative: the graph is small but recursion order must be stable."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for start in sorted(graph.modules):
        if start in index:
            continue
        work: list[tuple[str, int]] = [(start, 0)]
        while work:
            node, position = work.pop()
            if position == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            targets = sorted(graph.edges.get(node, ()))
            advanced = False
            for offset in range(position, len(targets)):
                target = targets[offset]
                if target not in index:
                    work.append((node, offset + 1))
                    work.append((target, 0))
                    advanced = True
                    break
                if target in on_stack:
                    low[node] = min(low[node], index[target])
            if advanced:
                continue
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])

    components.sort(key=lambda component: (-len(component), component))
    return components


def mutual_directory_pairs(graph: ModuleGraph) -> list[tuple[str, str]]:
    directed = {
        (directory_of(source), directory_of(target)) for source, targets in graph.edges.items() for target in targets
    }
    return sorted((a, b) for a, b in directed if a < b and (b, a) in directed)


def inbound_rank(graph: ModuleGraph, limit: int = 15) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for targets in graph.edges.values():
        for target in targets:
            counts[target] += 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def render(graph: ModuleGraph) -> str:
    """The committed document, byte-stable for a given source tree."""
    components = strongly_connected(graph)
    mutual = mutual_directory_pairs(graph)
    in_cycle = sum(len(component) for component in components)

    lines: list[str] = [
        "# Dependency Graph",
        "",
        "<!-- Generated by `scripts/dependency_graph.py --write`. Do not edit by hand. -->",
        "",
        "Module-level import edges between `sms_tool` modules, resolved from the AST",
        "(absolute and relative imports, aliases included). Function-local imports are",
        "counted separately: they are this repo's documented way to break a runtime",
        "cycle, so folding them into the cycle view would report a fix as a defect.",
        "",
        "`python scripts/dependency_graph.py` fails when this file and the source",
        "disagree, so the numbers cannot go stale. `import_layer_ratchet.py` remains",
        "the *gate* (per-pair, only-down); this file is the *view*.",
        "",
        "## Totals",
        "",
        "| Measure | Value |",
        "| --- | ---: |",
        f"| Modules | {len(graph.modules)} |",
        f"| Module-level import edges | {graph.module_edge_count()} |",
        f"| Delayed (function-local) edges | {graph.delayed_edge_count()} |",
        f"| Strongly connected components (size > 1) | {len(components)} |",
        f"| Modules inside a cycle | {in_cycle} |",
        f"| Mutually dependent directory pairs | {len(mutual)} |",
        "",
        "## Cycles",
        "",
    ]

    if components:
        lines += ["| Size | Modules |", "| ---: | --- |"]
        for component in components:
            lines.append(f"| {len(component)} | {'<br>'.join(f'`{name}`' for name in component)} |")
    else:
        lines.append("None.")

    lines += ["", "## Directory pairs with edges in both directions", ""]
    if mutual:
        lines += ["| Pair |", "| --- |"]
        lines += [f"| `{a}` ↔ `{b}` |" for a, b in mutual]
    else:
        lines.append("None.")

    lines += ["", "## Most imported modules", "", "| Module | Importers |", "| --- | ---: |"]
    lines += [f"| `{name}` | {count} |" for name, count in inbound_rank(graph)]

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and verify the sms_tool dependency graph.")
    parser.add_argument("--write", action="store_true", help="rewrite docs/current/dependency-graph.md")
    parser.add_argument("--detail", action="store_true", help="print every module-level edge")
    args = parser.parse_args(argv)

    graph = build()
    if args.detail:
        for source in sorted(graph.edges):
            for target in sorted(graph.edges[source]):
                print(f"  {source} -> {target}")
        return 0

    rendered = render(graph)
    if args.write:
        DOC.parent.mkdir(parents=True, exist_ok=True)
        DOC.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {DOC.relative_to(ROOT).as_posix()}")
        return 0

    if not DOC.exists():
        print(f"missing {DOC.relative_to(ROOT).as_posix()}; run --write", file=sys.stderr)
        return 1

    committed = DOC.read_text(encoding="utf-8")
    if committed != rendered:
        print(
            "docs/current/dependency-graph.md is stale; run `python scripts/dependency_graph.py --write`.",
            file=sys.stderr,
        )
        return 1

    print(
        "dependency graph OK "
        f"({len(graph.modules)} module(s), {graph.module_edge_count()} module-level edge(s), "
        f"{len(strongly_connected(graph))} cycle(s), {len(mutual_directory_pairs(graph))} mutual pair(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
