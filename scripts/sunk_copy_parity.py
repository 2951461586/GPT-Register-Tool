"""Parity guard for the "sunk copies" shared by ``upi_link`` and ``gen_link``.

Why this exists
---------------
``upi_link`` re-implements three helpers instead of importing them from
``paypal_link.gen_link``: the dependency direction is ``gen_pp_link ->
upi_link``, never the reverse (see the package docstring in
``sms_tool/upi_link/__init__.py``), so importing the canonical ``gen_link``
helpers here would close an import cycle. The copies live in the submodule that
owns each helper (``env`` / ``config`` / ``session``):

  * ``_emit``                          -- progress/error sink
  * ``_load_json``                     -- BOM- and shard-tolerant config loader
  * ``_normalize_hosted_checkout_url`` -- Stripe -> pay.openai.com host rewrite

A deliberate duplicate only stays safe while both copies are *identical*. A
one-sided edit (a new guard in one loader, a changed host in one normaliser) is
invisible at import time: both modules keep working, and the divergence only
surfaces later as a behaviour difference between the PayPal and UPI lanes.

``_method_cfg`` / ``_payment_stage_proxies_from_config`` are deliberately NOT
pairs: they are UPI-only. ``gen_link`` used to import them as an unused
re-export conduit; that import was removed (they have no PayPal counterpart).

What it checks
--------------
For each declared pair it parses both modules and compares the function bodies
after stripping docstrings -- docstrings are prose, and the two copies carry
different ones on purpose. Comments and whitespace are already invisible to the
AST. Any body difference is a parity violation and fails the gate.

The predicate is pinned by ``FIXTURES``, run on every invocation. A predicate
that over-normalises would report unrelated functions as identical; one that
under-normalises would report harmless docstring differences as drift. Both
directions are covered, so the guard cannot pass vacuously.

Standard library only: it runs as a pytest test and, when wired, as a git hook.

Usage:
    python scripts/sunk_copy_parity.py             # check (exit 1 on drift)
    python scripts/sunk_copy_parity.py --detail    # per-pair table
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (left file, right file, function name, why the copy exists)
PAIRS: tuple[tuple[str, str, str, str], ...] = (
    (
        "sms_tool/upi_link/env.py",
        "sms_tool/paypal_link/gen_link.py",
        "_emit",
        "progress/error sink shared by both payment lanes",
    ),
    (
        "sms_tool/upi_link/config.py",
        "sms_tool/paypal_link/gen_link.py",
        "_load_json",
        "shard-aware config loader; must stay BOM/shard-compatible both ways",
    ),
    (
        "sms_tool/upi_link/session.py",
        "sms_tool/paypal_link/gen_link.py",
        "_normalize_hosted_checkout_url",
        "Stripe checkout host rewrite shared by both payment lanes",
    ),
)

# (source_a, source_b, expected_identical?) -- pins the predicate both ways.
# See the module docstring: a parity predicate is a single point of failure.
FIXTURES: tuple[tuple[str, str, bool], ...] = (
    # identical bodies pair up
    ("def f(x):\n    return x + 1\n", "def f(x):\n    return x + 1\n", True),
    # a real body difference must NOT normalise away
    ("def f(x):\n    return x + 1\n", "def f(x):\n    return x - 1\n", False),
    # docstring-only difference is prose, not drift
    (
        'def f(x):\n    """alpha."""\n    return x + 1\n',
        'def f(x):\n    """beta."""\n    return x + 1\n',
        True,
    ),
    # ...but a docstring must not hide a real statement difference
    (
        'def f(x):\n    """alpha."""\n    return x + 1\n',
        'def f(x):\n    """alpha."""\n    return x + 2\n',
        False,
    ),
    # comments and whitespace are invisible to the AST
    ("def f(x):\n    return x + 1\n", "def f(x):\n    # comment\n    return x + 1\n", True),
    # a string-literal difference is a real difference
    ('def f():\n    return "a"\n', 'def f():\n    return "b"\n', False),
)


def _drop_docstrings(node: ast.AST) -> None:
    """Remove every first-statement string docstring from ``node``, in place.

    Mirrors ``extractor_parity_report._drop_docstrings``: only the first
    statement of a ``Module`` / ``FunctionDef`` / ``AsyncFunctionDef`` /
    ``ClassDef`` is dropped. A bare string expression that is not first is left
    alone -- there it is a real statement and dropping it would hide a
    difference rather than reveal one.
    """
    for child in ast.walk(node):
        body = getattr(child, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            body.pop(0)


def _function_signature(source: str, name: str) -> str | None:
    """Docstring-stripped AST dump for the top-level ``def name``, or ``None``.

    Comments and whitespace never reach the AST, so two bodies that differ only
    in formatting compare equal without any extra normalisation.
    """
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            _drop_docstrings(node)
            return ast.dump(node)
    return None


def predicates_are_trustworthy() -> list[str]:
    """Run ``FIXTURES`` and return a human-readable failure per broken case."""
    failures: list[str] = []
    for index, (source_a, source_b, expected) in enumerate(FIXTURES):
        got = _function_signature(source_a, "f") == _function_signature(source_b, "f")
        if got != expected:
            failures.append(f"fixture #{index}: expected identical={expected}, got {got}")
    return failures


def compare() -> list[dict]:
    """Return one record per declared pair, reading each module at most once."""
    sources: dict[str, str] = {}
    records: list[dict] = []
    for left, right, name, why in PAIRS:
        for path in (left, right):
            if path not in sources:
                sources[path] = (ROOT / path).read_text(encoding="utf-8")
        left_signature = _function_signature(sources[left], name)
        right_signature = _function_signature(sources[right], name)
        present = left_signature is not None and right_signature is not None
        records.append(
            {
                "function": name,
                "left": left,
                "right": right,
                "why": why,
                "present": present,
                "identical": present and left_signature == right_signature,
            }
        )
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sunk-copy parity guard")
    parser.add_argument("--detail", action="store_true", help="print a per-pair table")
    args = parser.parse_args(argv)

    fixture_failures = predicates_are_trustworthy()
    if fixture_failures:
        for line in fixture_failures:
            print(f"sunk-copy parity predicate is broken: {line}")
        return 2

    records = compare()
    missing = [r for r in records if not r["present"]]
    drift = [r for r in records if r["present"] and not r["identical"]]

    if args.detail:
        for record in records:
            state = "identical" if record["identical"] else ("MISSING" if not record["present"] else "DRIFT")
            print(f"  {state:9s}  {record['function']}  ({record['left']} <-> {record['right']})")

    if missing:
        for record in missing:
            print(
                f"sunk-copy parity FAILED: {record['function']} not found in both "
                f"{record['left']} and {record['right']}"
            )
        return 1

    if drift:
        for record in drift:
            print(
                f"sunk-copy drift: {record['function']} differs between "
                f"{record['left']} and {record['right']} ({record['why']})"
            )
        return 1

    print(f"sunk-copy parity OK ({len(records)} pair(s) identical)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
