"""Ratchet: the ``unittest.TestCase`` files may only shrink.

Why this exists
---------------
The suite has two styles side by side: 175 files declare ``unittest.TestCase``
and 261 use plain pytest functions. Both run (pytest collects unittest classes),
so no gate ever noticed the split -- and the split has a cost that shows up
exactly when a test fails:

* a ``unittest`` class gets **no pytest fixtures**, so ``monkeypatch``,
  ``tmp_path``, ``caplog`` and ``request.addfinalizer`` are unavailable and
  every shared setup becomes a hand-written ``setUp`` with hand-written teardown
  (which does not run when ``setUp`` itself raises);
* ``self.assertEqual`` reports a diff-free message pair, where bare ``assert`` is
  rewritten by pytest to show the intermediate values;
* ``unittest`` classes cannot be parametrised, so file-sharing fixtures are
  duplicated per class instead of being one function argument.

Converting 175 files in one commit is exactly the kind of change this repository
treats as unverifiable: a codemod that drops a ``setUp`` still leaves tests
passing (vacuously). So the debt is frozen per directory, may only go down, and
each directory migrates on its own -- the worked example is
``tests/config/test_logging_crash_hooks.py`` (2026-11-20), where the shared
``setUp`` became one ``hooks`` fixture with a ``yield`` + ``finally`` restore.

What counts
-----------
A file *declaring* a ``unittest.TestCase`` subclass (or a bare
``unittest.main()`` entry point) anywhere under ``tests/``. A file that merely
imports ``unittest.mock`` does not count.

Usage:
    python scripts/unittest_style_ratchet.py            # check (exit 1 on growth)
    python scripts/unittest_style_ratchet.py --detail   # per-directory table
    python scripts/unittest_style_ratchet.py --update-baseline

Output is ASCII-only: CI runs on a Windows runner whose stdout is cp1252.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

from _baseline import add_update_flag, print_failures, read_json, update_requested, write_json

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
BASELINE = Path(__file__).resolve().parent / "unittest_style_baseline.json"


def _is_unittest_file(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                if name == "TestCase":
                    return True
    return False


def scan(tests: Path = TESTS) -> dict[str, list[str]]:
    """Directory (relative to tests/) -> sorted file names that still use unittest."""
    found: dict[str, list[str]] = {}
    for path in sorted(tests.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        if not _is_unittest_file(tree):
            continue
        group = path.parent.relative_to(tests).as_posix() or "."
        found.setdefault(group, []).append(path.name)
    return found


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    raw = read_json(path)
    if raw.get("schema") != "unittest_style.v1":
        raise SystemExit(f"{path.name}: unexpected schema {raw.get('schema')!r}")
    return {str(key): int(value) for key, value in raw["files"].items()}


def build_baseline(found: dict[str, list[str]]) -> dict[str, object]:
    return {
        "schema": "unittest_style.v1",
        "files": {group: len(names) for group, names in sorted(found.items())},
    }


def violations(found: dict[str, list[str]], baseline: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for group in sorted(set(found) | set(baseline)):
        current = len(found.get(group, []))
        allowed = baseline.get(group)
        if allowed is None:
            problems.append(
                f"tests/{group}: {current} unittest file(s) but no baseline entry "
                "(new directory -> add it, or write the files in pytest style)"
            )
        elif current > allowed:
            # The baseline stores counts, not names, so the message names the
            # files from the fresh scan in that directory instead of guessing
            # which one is new.
            problems.append(
                f"tests/{group}: {current} unittest file(s) > baseline {allowed} "
                f"(+{current - allowed}); write new files in pytest style, or convert: " + ", ".join(found[group][:3])
            )
    return problems


def stale_entries(found: dict[str, list[str]], baseline: dict[str, int]) -> list[str]:
    return [
        f"tests/{group}: baseline allows {baseline[group]} but 0 remain -- tighten the baseline"
        for group in sorted(baseline)
        if len(found.get(group, [])) < baseline[group]
    ]


def render(found: dict[str, list[str]]) -> str:
    lines = [f"{'directory':<24}{'unittest files':>15}"]
    lines.append("-" * 39)
    for group in sorted(found):
        lines.append(f"tests/{group:<18}{len(found[group]):>15}")
    lines.append("-" * 39)
    lines.append(f"{'total':<24}{sum(len(names) for names in found.values()):>15}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze the unittest.TestCase file count per test directory.")
    parser.add_argument("--detail", action="store_true", help="print the per-directory table")
    add_update_flag(parser)
    args = parser.parse_args(argv)

    found = scan()
    if update_requested(args):
        write_json(BASELINE, build_baseline(found))
        print(f"baseline updated: unittest_style_baseline.json ({sum(len(n) for n in found.values())} files)")
        return 0

    baseline = load_baseline()
    if args.detail:
        print(render(found))
        print()

    problems = violations(found, baseline)
    if problems:
        return print_failures("unittest-style ratchet FAILED:", problems)

    for note in stale_entries(found, baseline):
        print(f"note: {note}")
    print(
        f"unittest-style ratchet OK ({sum(len(n) for n in found.values())} unittest file(s) across {len(found)} dir(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
