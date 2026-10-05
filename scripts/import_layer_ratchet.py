"""Ratchet: cross-directory import edges inside ``sms_tool`` must not grow.

Why this exists
---------------
``sms_tool`` is one package whose 287 modules are grouped into 13 subpackages
(``accounts/``, ``providers/``, ``pay_link/``, ...) plus the ~200 top-level
modules. ``docs/architecture.md`` ("Dependency Direction") fixes a layer model:
UI -> CLI/command adapters -> application workflows -> domain contracts ->
provider/persistence adapters, with imports pointing inward only and a lower
layer never importing a higher one.

That model was never *measured*. On 2026-10-04 the review graph collapsed the
package to directory granularity and reported **one** strongly connected
component spanning 14 directories and 470 import statements -- the whole core,
with no enforceable layering at all. Every refactor that crosses a subpackage
boundary was therefore invisible, and the component could only grow.

Measuring it first showed the debt is far smaller than 470 edges, and that most
of the "cycle" is deliberate design that must **not** be broken:

* 29 of the edges are **function-local (delayed) imports** -- the documented way
  this codebase breaks a runtime import cycle. They are counted by
  ``scripts/delayed_import_ratchet.py``, not here.
* 5 edges are module-level **wildcard re-exports** (``storage.py`` ->
  ``store``, ``paypal_auto.py`` -> ``paypal``, ...) and 17 more are the
  ``mailbox_*`` / ``outlook_imap`` **compatibility facades**. Both are recorded
  decisions (ADR-0009, ``docs/architecture.md`` Rule 15); deleting them would
  break ~30 importers to satisfy a metric.
* ``auth_flow/deps.py`` is a **dependency-injection seam** that a test patches
  by attribute name; replacing its imports with direct ones would silently
  break patchability (the defect ``docs/CONTEXT.md`` already records for the
  payment-capability seam).

So the enforceable number is the **module-level** cross-directory edge count:
441 statements over 43 ordered directory pairs, of which 30 pairs are mutually
dependent.

What it checks
--------------
For every ``.py`` under ``sms_tool/`` (excluding ``_vendor/`` and
``__pycache__/``) it resolves each import to the module it actually names --
absolute (``sms_tool.a.b``) or relative (``from ..a import b``), including the
package-relative case that a naive resolver gets wrong: an import inside
``sms_tool/upi_link/__init__.py`` belongs to ``sms_tool/upi_link``, **not** to
``sms_tool``. Mis-classifying that one case inflates ``sms_tool`` by 12 phantom
edges.

Each **module-scope** import that crosses a directory boundary is one edge,
keyed ``source_directory -> target_directory``. The per-pair count is frozen in
``import_layer_baseline.json`` and may only go **down**. Imports inside a
function or class body are counted separately as ``delayed`` and are explicitly
**not** gated here: converting a cycle edge into a delayed import is the
sanctioned remedy, and freezing it would forbid the fix. ``delayed`` edges are
reported so a shift from module-scope to delayed is visible in ``--detail``.

Per-pair on purpose, not one grand total: with a single number, wiring one pair
would mask a new back-edge added to another. The trade-off is stated so it is
not mistaken for full coverage -- moving an import *within* a pair (say from
``cli.py`` to ``registration.py``, both ``sms_tool`` -> ``commands``) leaves the
pair count unchanged. That is deliberate: the unit of architecture here is the
directory relationship, and a same-pair redistribution does not worsen it. The
per-source-module breakdown is printed by ``--detail`` for anyone who needs it.

The resolver is pinned by ``FIXTURES`` on every invocation: it builds a
throwaway module tree and asserts the exact edge set, in both directions
(absolute/relative/boundary-crossing/same-dir/self/delayed/comment). A resolver
that over- or under-matches fails its own fixtures before it is allowed to judge
the tree.

Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "import_layer_baseline.json"

PKG = "sms_tool"
EXCLUDED_DIRS = {"__pycache__", "_vendor"}

#: Subpackages that are their own directory node. Anything else directly under
#: ``sms_tool/`` collapses into the ``sms_tool`` node.
SUBPACKAGES = (
    "accounts",
    "auth_flow",
    "cli_parsers",
    "commands",
    "geo",
    "pay_link",
    "paypal",
    "paypal_link",
    "providers",
    "registration_drivers",
    "sentinel",
    "store",
    "upi_link",
)

MODULE_SCOPE = "module"
DELAYED_SCOPE = "delayed"


@dataclass
class Edge:
    """One resolved cross-directory import statement."""

    source_dir: str
    target_dir: str
    source_module: str
    line: int
    scope: str
    text: str

    @property
    def pair(self) -> str:
        return f"{self.source_dir} -> {self.target_dir}"


@dataclass
class Analysis:
    edges: list[Edge] = field(default_factory=list)

    @property
    def module_edges(self) -> list[Edge]:
        return [e for e in self.edges if e.scope == MODULE_SCOPE]

    @property
    def delayed_edges(self) -> list[Edge]:
        return [e for e in self.edges if e.scope == DELAYED_SCOPE]

    def pair_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter(e.pair for e in self.module_edges)
        return {pair: counts[pair] for pair in sorted(counts)}

    def mutual_pairs(self) -> list[tuple[str, str]]:
        """Directory pairs where both directions carry a module-level edge."""
        directed = {(e.source_dir, e.target_dir) for e in self.module_edges}
        return sorted((a, b) for a, b in directed if a < b and (b, a) in directed)

    def minority_edges(self) -> list[Edge]:
        """Module-level edges pointing the *weaker* way across a mutual pair."""
        counts = Counter((e.source_dir, e.target_dir) for e in self.module_edges)
        out: list[Edge] = []
        for a, b in self.mutual_pairs():
            ab, ba = counts[(a, b)], counts[(b, a)]
            loser = (b, a) if ab >= ba else (a, b)
            out.extend(e for e in self.module_edges if (e.source_dir, e.target_dir) == loser)
        return out

    def totals(self) -> dict[str, int]:
        module_edges = self.module_edges
        return {
            "total_module_edges": len(module_edges),
            "ordered_pairs": len(self.pair_counts()),
            "mutual_pairs": len(self.mutual_pairs()),
            "minority_edges": len(self.minority_edges()),
            "delayed_cross_dir_edges": len(self.delayed_edges),
        }


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("").as_posix()
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _directory_of(module: str, is_package: bool) -> str:
    parts = module.split(".")
    if len(parts) >= 2 and parts[1] in SUBPACKAGES:
        return f"{PKG}/{parts[1]}"
    return PKG


def _resolve_relative(module: str, is_package: bool, base: str | None, level: int) -> str:
    """Resolve ``from <level dots><base> import ...`` to an absolute module name."""
    parts = module.split(".")
    context = parts if is_package else parts[:-1]
    # level 1 == the current package; each extra dot walks one package up.
    if level > 1:
        context = context[: len(context) - (level - 1)]
    resolved = list(context)
    if base:
        resolved += base.split(".")
    return ".".join(resolved)


def _index(root: Path) -> tuple[list[Path], dict[str, Path], dict[str, bool]]:
    """(files, module -> path, module -> is_package) for one tree."""
    files: list[Path] = []
    package_root = root / PKG
    for path in sorted(package_root.rglob("*.py")):
        if EXCLUDED_DIRS & set(path.parts):
            continue
        files.append(path)
    modules = {_module_name(root, p): p for p in files}
    is_package = {name: path.name == "__init__.py" for name, path in modules.items()}
    return files, modules, is_package


def _resolve_any(spec: str, modules: dict[str, Path]) -> str | None:
    """Longest existing module prefix of ``spec`` (imports may name a submodule)."""
    parts = spec.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in modules:
            return candidate
    return None


def analyze(root: Path = ROOT) -> Analysis:
    """Collect every cross-directory import edge under ``root/sms_tool``."""
    files, modules, is_package = _index(root)
    analysis = Analysis()

    for path in files:
        module = _module_name(root, path)
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        # Any import nested inside a function/class body is a delayed import.
        nested: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        nested.add(id(child))

        lines = source.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                specs: list[tuple[str | None, int]] = [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                specs = [(node.module, node.level)]
            else:
                continue

            for base, level in specs:
                target_spec = _resolve_relative(module, is_package[module], base, level) if level else (base or "")
                target = _resolve_any(target_spec, modules)
                if target is None or target == module:
                    continue
                source_dir = _directory_of(module, is_package[module])
                target_dir = _directory_of(target, is_package[target])
                if source_dir == target_dir:
                    continue
                analysis.edges.append(
                    Edge(
                        source_dir=source_dir,
                        target_dir=target_dir,
                        source_module=module,
                        line=node.lineno,
                        scope=DELAYED_SCOPE if id(node) in nested else MODULE_SCOPE,
                        text=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else "",
                    )
                )
    return analysis


# --------------------------------------------------------------------------
# Fixtures: a throwaway module tree with a known edge set, used to pin the
# resolver before it is trusted against the real package.
# --------------------------------------------------------------------------

FIXTURE_TREE: dict[str, str] = {
    "sms_tool/__init__.py": "",
    # sms_tool -> sms_tool/geo, twice (absolute, two statements).
    "sms_tool/alpha.py": ("from sms_tool.geo import helper\nfrom sms_tool.geo.helper import thing\n"),
    "sms_tool/beta.py": "VALUE = 1\n",
    # Delayed (function-local) cross-directory edge: sms_tool -> sms_tool/accounts.
    "sms_tool/gamma.py": ("def f():\n    from .accounts import helper\n    return helper\n"),
    # Wildcard re-export across a boundary: sms_tool -> sms_tool/accounts.
    "sms_tool/wild.py": "from .accounts import *  # noqa: F401,F403\n",
    # Package-relative self-imports must NOT become edges (the 12-phantom-edge bug).
    "sms_tool/accounts/__init__.py": (
        "from . import helper\nfrom .helper import thing\nfrom .. import alpha\n"  # level 2 -> sms_tool
    ),
    "sms_tool/accounts/helper.py": (
        "from ..geo import helper as g\n"  # accounts -> geo
        "thing = 1\n"
    ),
    "sms_tool/geo/__init__.py": "",
    "sms_tool/geo/helper.py": (
        "from ..accounts import helper as a\n"  # geo -> accounts (closes the mutual pair)
    ),
    # A stdlib import and a prose mention must never be counted.
    "sms_tool/accounts/notes.py": ("import os\n# see sms_tool.geo.helper for the flow\nVALUE = os.sep\n"),
    "sms_tool/_vendor/skipme.py": "from sms_tool import alpha\n",  # excluded
}

#: (source_dir, target_dir, count) -- module-scope cross-directory edges only.
FIXTURE_EXPECTED: tuple[tuple[str, str, int], ...] = (
    ("sms_tool", "sms_tool/geo", 2),
    ("sms_tool", "sms_tool/accounts", 1),  # wild.py only; gamma.py is delayed
    ("sms_tool/accounts", "sms_tool", 1),
    ("sms_tool/accounts", "sms_tool/geo", 1),
    ("sms_tool/geo", "sms_tool/accounts", 1),
)

#: Pairs that must NOT appear: same-directory and package-relative self-imports.
FIXTURE_FORBIDDEN: tuple[tuple[str, str], ...] = (
    ("sms_tool", "sms_tool"),
    ("sms_tool/accounts", "sms_tool/accounts"),
    ("sms_tool/geo", "sms_tool/geo"),
)

#: Delayed (function-local) cross-directory edges in the fixture tree.
FIXTURE_EXPECTED_DELAYED: tuple[tuple[str, str, int], ...] = (
    ("sms_tool", "sms_tool/accounts", 1),  # gamma.py f() -> accounts.helper
)


def _build_fixture_tree(base: Path) -> Path:
    for rel, text in FIXTURE_TREE.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return base


def resolver_is_trustworthy() -> str | None:
    """Return a failure description if the resolver fails its own fixtures."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = _build_fixture_tree(Path(tmp) / "repo")
        analysis = analyze(base)

        counts = Counter((e.source_dir, e.target_dir) for e in analysis.module_edges)
        for source_dir, target_dir, expected in FIXTURE_EXPECTED:
            actual = counts[(source_dir, target_dir)]
            if actual != expected:
                problems.append(f"{source_dir} -> {target_dir}: expected {expected}, got {actual}")
        for source_dir, target_dir in FIXTURE_FORBIDDEN:
            if counts[(source_dir, target_dir)]:
                problems.append(
                    f"same-directory edge {source_dir} -> {target_dir} was counted ({counts[(source_dir, target_dir)]})"
                )

        delayed = Counter((e.source_dir, e.target_dir) for e in analysis.delayed_edges)
        for source_dir, target_dir, expected in FIXTURE_EXPECTED_DELAYED:
            actual = delayed[(source_dir, target_dir)]
            if actual != expected:
                problems.append(f"delayed {source_dir} -> {target_dir}: expected {expected}, got {actual}")

        # The excluded tree must contribute nothing at all.
        if any("_vendor" in e.source_module for e in analysis.edges):
            problems.append("_vendor imports were not excluded")

        # A resolver that finds nothing manufactures false confidence.
        if not analysis.module_edges:
            problems.append("resolver found no module edges in the fixture tree")
    return "; ".join(problems) if problems else None


def load_baseline(path: Path | None = None) -> dict[str, object]:
    if path is None:
        path = BASELINE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        # Corrupt baseline -> empty -> every current pair is "grown" against an
        # allowance of 0, so the ratchet fails loudly rather than passing blind.
        return {}
    return data if isinstance(data, dict) else {}


def _baseline_pairs(baseline: dict[str, object]) -> dict[str, int]:
    pairs = baseline.get("pairs")
    if not isinstance(pairs, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in pairs.items():
        try:
            out[str(key)] = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return out


def _baseline_int(baseline: dict[str, object], key: str) -> int | None:
    value = baseline.get(key)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ratchet on cross-directory import edges inside sms_tool.")
    parser.add_argument("--detail", action="store_true", help="print per-pair and per-module tables")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline (only after LOWERING an edge, i.e. a real convergence)",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    broken = resolver_is_trustworthy()
    if broken is not None:
        print(f"SELF-TEST FAILED: {broken}", file=sys.stderr)
        print("import-layer-ratchet: refusing to report with a broken resolver.", file=sys.stderr)
        return 1

    analysis = analyze(args.root)
    counts = analysis.pair_counts()
    totals = analysis.totals()

    if args.detail:
        print("=== module-level cross-directory edges, by pair ===")
        for pair, count in counts.items():
            print(f"  {count:4d}  {pair}")
        print("\n=== per source module (module-scope only) ===")
        per_module: Counter[str] = Counter()
        for edge in analysis.module_edges:
            per_module[f"{edge.source_module} -> {edge.target_dir}"] += 1
        for key in sorted(per_module):
            print(f"  {per_module[key]:4d}  {key}")
        print("\n=== minority-direction edges (the reducible core) ===")
        for edge in analysis.minority_edges():
            print(f"  {edge.source_module}:{edge.line}  {edge.pair}")
        print("\n=== totals ===")
        for key, value in totals.items():
            print(f"  {key:26s} {value}")

    if args.update_baseline:
        payload = {
            "pairs": counts,
            **totals,
        }
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(
            f"baseline updated: {BASELINE.name} ({totals['total_module_edges']} edges, {totals['ordered_pairs']} pairs)"
        )
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        print("run: python scripts/import_layer_ratchet.py --update-baseline", file=sys.stderr)
        return 2

    baseline = load_baseline(BASELINE)
    allowed_pairs = _baseline_pairs(baseline)

    grown: list[tuple[str, int, int]] = []
    for pair in sorted(set(counts) | set(allowed_pairs)):
        allowed = allowed_pairs.get(pair, 0)
        current = counts.get(pair, 0)
        if current > allowed:
            grown.append((pair, current, allowed))

    for key in ("total_module_edges", "mutual_pairs", "minority_edges"):
        allowed = _baseline_int(baseline, key)
        if allowed is not None and totals[key] > allowed:
            grown.append((key, totals[key], allowed))

    if grown:
        for name, current, allowed in grown:
            print(
                f"{name}: {current} > baseline {allowed} (+{current - allowed}). "
                f"Import through the layer that owns the name, move the shared helper into a "
                f"leaf module both sides may import, or -- if the edge is a documented "
                f"facade/DI seam -- convert it to a function-local import (counted by "
                f"delayed_import_ratchet.py, not frozen here).",
                file=sys.stderr,
            )
        return 1

    print(
        f"import-layer ratchet OK ({totals['total_module_edges']} module-level edge(s) over "
        f"{totals['ordered_pairs']} pair(s); {totals['mutual_pairs']} mutual, "
        f"{totals['minority_edges']} minority; {totals['delayed_cross_dir_edges']} delayed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
