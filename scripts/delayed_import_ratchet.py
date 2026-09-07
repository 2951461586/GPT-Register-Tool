"""Ratchet for function-level (delayed) imports inside ``sms_tool/``.

Why this exists
---------------
``sms_tool`` carries a few hundred imports that are executed inside a function
body instead of at module top level. Some are load-bearing -- they break a real
import cycle, or defer an optional dependency so a missing extra does not take
down the CLI at startup. But the pattern is invisible: nothing stops the next
one from being added "just to be safe", and a delayed import is also the one
form of import that hides a cycle from any top-level dependency scan.

This is a **ratchet, not a cleanup task**. It records today's count and fails
if the number goes up. It deliberately does *not* demand that existing ones be
removed -- that would turn a guard into a blocked CI pipeline. Lowering the
baseline is a conscious act:

    python scripts/delayed_import_ratchet.py --update-baseline

...which should only ever be run after deleting imports, never after adding
them. ``--update-baseline`` prints exactly what changed so a reviewer can see
whether the number moved for the right reason.

Standard library only: it runs as a pytest test and as a git hook.

Usage:
    python scripts/delayed_import_ratchet.py                 # check vs baseline
    python scripts/delayed_import_ratchet.py --detail        # per-file counts
    python scripts/delayed_import_ratchet.py --update-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sms_tool"
BASELINE = Path(__file__).resolve().parent / "delayed_import_baseline.json"

_SKIP_DIRS = {"__pycache__"}


def _delayed_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """Import statements nested inside a function body.

    A class body is *not* delayed: it runs at import time, so it behaves like a
    top-level import for cycle and startup-cost purposes.
    """
    found: list[ast.Import | ast.ImportFrom] = []

    def walk(node: ast.AST, in_function: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_function = True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and in_function:
            found.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child, in_function)

    walk(tree, False)
    return found


def count_delayed_imports(root: Path = PACKAGE) -> tuple[int, dict[str, int]]:
    """Return ``(total, {relative_path: count})`` for one package tree."""
    per_file: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"skip {path}: {exc}", file=sys.stderr)
            continue
        count = len(_delayed_imports(tree))
        if count:
            # as_posix so the baseline is stable across platforms; only the
            # total gates the check, but a per-file diff is useless if the
            # separators flip between Windows and Linux CI.
            per_file[path.relative_to(root.parent).as_posix()] = count
    return sum(per_file.values()), per_file


def load_baseline(path: Path = BASELINE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--detail", action="store_true", help="print per-file counts")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline with today's count (only after deleting imports)",
    )
    parser.add_argument("--root", type=Path, default=PACKAGE)
    args = parser.parse_args(argv)

    total, per_file = count_delayed_imports(args.root)

    if args.update_baseline:
        previous = load_baseline() if BASELINE.exists() else {"total": 0, "per_file": {}}
        BASELINE.write_text(
            json.dumps({"total": total, "per_file": per_file}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        delta = total - int(previous.get("total") or 0)
        print(f"baseline updated: {previous.get('total')} -> {total} ({delta:+d})")
        return 0

    if args.detail:
        for name, count in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"{count:4d}  {name}")
        print(f"{total:4d}  TOTAL")

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        return 2

    baseline = load_baseline()
    allowed = int(baseline.get("total") or 0)
    if total > allowed:
        print(
            f"delayed-import ratchet: {total} > baseline {allowed} "
            f"(+{total - allowed}). Delete an import or justify a baseline bump.",
            file=sys.stderr,
        )
        new = {k: v for k, v in per_file.items() if v > int(baseline.get("per_file", {}).get(k) or 0)}
        for name in sorted(new):
            print(f"  grew: {name} {baseline['per_file'].get(name, 0)} -> {per_file[name]}", file=sys.stderr)
        return 1
    print(f"delayed-import ratchet OK: {total} <= baseline {allowed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
