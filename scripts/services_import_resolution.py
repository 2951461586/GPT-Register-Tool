"""Resolve ``from common.X import ...`` under ``services/protocol-payment``.

Why this exists
---------------
``services/protocol-payment`` is a directory of standalone scripts, not a
package: the name contains a hyphen, so nothing can import it as
``services.protocol-payment``. Each extractor instead puts the directory on
``sys.path`` at runtime and imports ``common.*`` as a top-level package
(``pyrightconfig.json`` mirrors that with an ``executionEnvironments`` entry).

Static analysers that do not know about that extra import root see every
``common.*`` import as unresolved, and a review graph then reports the modules
under ``common/`` as dead code. The 2026-09-22 and 2026-10-04 scans both had to
refute that by hand. This gate removes the false positive instead: it resolves
the imports with the *same* rule the runtime uses, so the inventory can be
checked rather than guessed.

What it checks
--------------
1. Every ``common.*`` import in the tree resolves to a real module under
   ``common/``. A typo or a deleted helper fails here instead of at runtime.
2. Every ``common/*.py`` module is reachable from at least one extractor.
   A module nothing reaches is either genuinely dead or reached by a mechanism
   this gate cannot see; both need a decision, so it fails rather than guesses.

Usage:
    python scripts/services_import_resolution.py            # check
    python scripts/services_import_resolution.py --detail   # print the edge set
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "services" / "protocol-payment"
COMMON_DIR = PROTOCOL_ROOT / "common"
EXCLUDED_DIRS = {"__pycache__", "_vendor", "dumps"}

#: The import root every extractor installs on ``sys.path`` at runtime.
IMPORT_ROOT = "common"


def iter_sources(root: Path | None = None) -> list[Path]:
    """Every ``.py`` file under ``root``, excluding generated/vendored trees.

    ``root`` resolves at call time: a default argument would bind the real tree
    once and ignore a monkeypatched module global (the trap ``file_loading``
    already documents for this repo).
    """
    base = PROTOCOL_ROOT if root is None else root
    return [path for path in sorted(base.rglob("*.py")) if not (EXCLUDED_DIRS & set(path.parts))]


def defined_modules(common_dir: Path | None = None) -> set[str]:
    """``common`` plus every module name it exposes (``common.x``, ``common.pkg``)."""
    common_dir = COMMON_DIR if common_dir is None else common_dir
    names = {IMPORT_ROOT}
    for path in sorted(common_dir.rglob("*.py")):
        if EXCLUDED_DIRS & set(path.parts):
            continue
        rel = path.relative_to(common_dir).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.add(".".join([IMPORT_ROOT, *parts]))
    return names


def _specs(node: ast.AST, source_module: str | None, is_package: bool) -> list[tuple[str, bool]]:
    """``(spec, required)`` pairs an import statement names.

    ``required`` marks the module part of the statement: it must exist as a
    module, otherwise the import fails at runtime. Aliases are attributes or
    submodules, so a missing one only means "not an edge".
    """
    if isinstance(node, ast.Import):
        return [(alias.name, True) for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        if not source_module:
            return []
        parts = source_module.split(".")
        if not is_package:
            # A module file's ``level=1`` is its *parent* package, not itself.
            parts = parts[:-1]
        context = parts[: len(parts) - (node.level - 1)]
        base = ".".join(context + ([node.module] if node.module else []))
        return [(base, True)] if base else []
    if not node.module:
        return []
    specs = [(node.module, True)]
    specs += [(f"{node.module}.{alias.name}", False) for alias in node.names]
    return specs


def common_imports(
    path: Path, source_module: str | None = None, is_package: bool = False
) -> list[tuple[str, int, bool]]:
    """``(spec, lineno, required)`` for every import that names the ``common`` root."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    found: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        for spec, required in _specs(node, source_module, is_package):
            if spec == IMPORT_ROOT or spec.startswith(IMPORT_ROOT + "."):
                found.append((spec, getattr(node, "lineno", 0), required))
    return found


def resolve(spec: str, names: set[str], *, exact: bool) -> str | None:
    """Resolve ``spec`` against the module set; ``exact`` forbids prefix fallback."""
    if exact:
        return spec if spec in names else None
    parts = spec.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in names:
            return candidate
    return None


def analyze(
    root: Path | None = None, common_dir: Path | None = None
) -> tuple[list[tuple[Path, int, str]], dict[str, set[str]], dict[str, set[str]], list[str]]:
    """Return (unresolved, extractor -> targets, common module -> targets, orphans)."""
    root = PROTOCOL_ROOT if root is None else root
    common_dir = COMMON_DIR if common_dir is None else common_dir
    names = defined_modules(common_dir)
    unresolved: list[tuple[Path, int, str]] = []
    entry_map: dict[str, set[str]] = defaultdict(set)
    edges: dict[str, set[str]] = defaultdict(set)

    for path in iter_sources(root):
        source_module = None
        if path.parent == common_dir:
            source_module = f"{IMPORT_ROOT}.{path.stem}"
        for spec, line, required in common_imports(path, source_module, path.name == "__init__.py"):
            target = resolve(spec, names, exact=required)
            if target is None:
                if required:
                    unresolved.append((path, line, spec))
                continue
            if target == source_module:
                continue
            if source_module is None:
                entry_map[path.relative_to(root).as_posix()].add(target)
            else:
                edges[source_module].add(target)

    entry = {target for targets in entry_map.values() for target in targets}
    reachable = set(entry)
    stack = list(entry)
    while stack:
        module = stack.pop()
        for target in edges.get(module, ()):
            if target not in reachable:
                reachable.add(target)
                stack.append(target)

    declared = {name for name in names if name != IMPORT_ROOT}
    orphans = sorted(declared - reachable)
    return unresolved, entry_map, edges, orphans


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve the common.* import root under services/protocol-payment.")
    parser.add_argument("--detail", action="store_true", help="print the resolved edge set")
    args = parser.parse_args(argv)

    if not PROTOCOL_ROOT.is_dir():
        print(f"missing tree: {PROTOCOL_ROOT}", file=sys.stderr)
        return 2

    unresolved, entry_map, edges, orphans = analyze()
    entry_targets = {target for targets in entry_map.values() for target in targets}
    if args.detail:
        for source in sorted(entry_map):
            print(f"  {source} -> {', '.join(sorted(entry_map[source]))}")
        for source in sorted(edges):
            print(f"  {source} -> {', '.join(sorted(edges[source]))}")
        print(f"  extractors -> {len(entry_targets)} module(s) via common")

    if unresolved:
        for path, line, spec in unresolved:
            try:
                where = path.relative_to(ROOT).as_posix()
            except ValueError:
                where = path.as_posix()
            print(f"{where}:{line}: unresolved import {spec}", file=sys.stderr)
        return 1

    if orphans:
        print(
            "services/protocol-payment: module(s) no extractor reaches: " + ", ".join(orphans),
            file=sys.stderr,
        )
        print(
            "Delete it, or import it from the extractor that is supposed to use it. "
            "The resolver only follows `from common...` imports, the same rule the "
            "runtime sys.path bootstrap uses.",
            file=sys.stderr,
        )
        return 1

    print(
        "services import resolution OK "
        f"({len(entry_map)} extractor(s) -> {len(entry_targets)} common module(s), "
        f"{len(edges)} common->common edge(s), 0 unresolved, 0 orphan)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
