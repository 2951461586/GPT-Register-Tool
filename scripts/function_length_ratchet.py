"""Ratchet for over-long functions -- the one quality axis no gate measured.

Why this exists
---------------
Every other shape defect in this repository has a gate: unused imports
(``unused_import_ratchet``), bare ``print`` (``bare_print_ratchet``), delayed
imports (``delayed_import_ratchet``), layer direction (``import_layer_ratchet``),
provider duplication (``provider_decoupling_ratchet``), config-key drift,
endpoint literals, .NET warnings.  Function **length** had none, and it is the
cheapest proxy for the defect the splits keep running into: a body long enough
that nobody reads it end to end, so a guard clause lands in the wrong place and
a return path becomes unreachable.

Measured 2026-10-23 over ``sms_tool/``, ``services/`` and ``scripts/`` (the three
production Python trees) with ``THRESHOLD = 150`` lines per function body: **63
functions**.  The distribution is steep, which is why the threshold is not a
style preference:

| lines | functions |
| ---: | ---: |
| >= 100 | 154 |
| >= 120 | 109 |
| >= 150 | **63** |
| >= 200 | 27 |
| >= 300 | 9 |

Numbers are ``end_lineno - lineno + 1`` (decorators and the signature count, a
body's docstring counts), so the metric is reproducible from the AST alone and
does not depend on a formatter.

Why a ratchet and not a lint rule
---------------------------------
``ruff`` does not measure body length, and pi-lens only reports what a scan
happened to look at.  A threshold that fails the build outright cannot land
today -- the 63 are real, and several are load-bearing protocol flows whose
split needs its own plan.  So: freeze them per function, fail on growth.

Why per function and not a total
--------------------------------
Same argument as ``unused_import_ratchet``: a global count lets a split in one
file pay for a 300-line monster in another.  The baseline key is
``relative/path.py::Qualname``, where ``Qualname`` is ``Class.method`` (nested
functions are joined with ``.<locals>.``), so a rename shows up as "one removed,
one new" and cannot be hidden.

Growth is never silent
----------------------
* a **new** function over the threshold fails;
* a **grown** function fails;
* a **shrunk** one passes and the tool prints how many baselines could be
  tightened;
* ``--update-baseline`` adopts shrinkages freely but refuses to adopt a new or
  grown function unless ``--reason`` explains it.  The reason is stored in the
  baseline next to the entry, so the next reader sees why 214 lines are
  acceptable instead of finding only a number.

Usage
-----
```bash
python scripts/function_length_ratchet.py                  # check
python scripts/function_length_ratchet.py --detail         # list every frozen function
python scripts/function_length_ratchet.py --update-baseline               # adopt shrinkages
python scripts/function_length_ratchet.py --update-baseline --reason "..."  # adopt growth
```
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "function_length_baseline.json"

#: Function bodies at or above this length are frozen.  Changing it is a
#: deliberate act: the baseline records it and a mismatch fails the check, so a
#: threshold edit always comes with a re-measured baseline in the same commit.
THRESHOLD = 150

#: Trees measured.  ``tests/`` is deliberately out of scope: procedural fixtures
#: legitimately run long, and the 3 test functions this would add are not the
#: defect class (a test body that big is a data table, not an unreadable flow).
SOURCE_DIRS = ("sms_tool", "services", "scripts")

#: Vendored code is pinned by digest elsewhere; its style is not ours to change.
SKIP_DIR_NAMES = frozenset({"_vendor", "__pycache__", ".venv", "node_modules", ".git"})


def _iter_sources() -> list[Path]:
    sources: list[Path] = []
    for top in SOURCE_DIRS:
        base = ROOT / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_DIR_NAMES & set(path.parts):
                continue
            sources.append(path)
    return sources


def functions_in(path: Path, *, threshold: int = THRESHOLD) -> dict[str, int]:
    """``Qualname -> length`` for every function at or above ``threshold``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return {}
    found: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                length = (child.end_lineno or child.lineno) - child.lineno + 1
                if length >= threshold:
                    found[name] = length
                # Keep walking: a nested ``def`` is its own body to judge, and a
                # long parent does not excuse it.
                visit(child, f"{name}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def collect(threshold: int = THRESHOLD) -> dict[str, int]:
    """``relative/path.py::Qualname -> length`` for the whole measured tree."""
    current: dict[str, int] = {}
    for path in _iter_sources():
        rel = path.relative_to(ROOT).as_posix()
        for qualname, length in functions_in(path, threshold=threshold).items():
            current[f"{rel}::{qualname}"] = length
    return dict(sorted(current.items()))


def evaluate(
    current: dict[str, int], baseline: dict, *, threshold: int = THRESHOLD
) -> tuple[list[tuple[str, str, int | None, int]], list[str]]:
    """Return ``(failures, tightened)``; pure, so the tests can drive it.

    ``failures`` entries are ``(kind, key, allowed, length)`` with ``kind`` one
    of ``NEW`` / ``GROWN``; ``allowed`` is ``None`` for a new function.  A
    structured result keeps the ``--update-baseline`` code from having to parse
    its own human-readable output (it did, and the key kept the trailing colon).
    """
    frozen = dict(baseline.get("functions") or {})
    failures: list[tuple[str, str, int | None, int]] = []
    tightened: list[str] = []
    for key, length in current.items():
        allowed = frozen.get(key)
        if allowed is None:
            failures.append(("NEW", key, None, length))
        elif length > allowed:
            failures.append(("GROWN", key, allowed, length))
        elif length < allowed:
            tightened.append(f"  SHRUNK   {key}: {allowed} -> {length} lines")
    return failures, tightened


def describe(failures: list[tuple[str, str, int | None, int]]) -> list[str]:
    lines = []
    for kind, key, allowed, length in failures:
        if allowed is None:
            lines.append(f"  {kind:<8} {key}: {length} lines (threshold {THRESHOLD})")
        else:
            lines.append(f"  {kind:<8} {key}: {allowed} -> {length} lines")
    return lines


def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}


def _write_baseline(stored: dict[str, int], reasons: dict[str, str], threshold: int) -> None:
    payload = {
        "threshold": threshold,
        "total": len(stored),
        "functions": dict(sorted(stored.items())),
        "reasons": dict(sorted(reasons.items())),
    }
    # ``newline="\n"`` is mandatory: ``Path.write_text`` translates to
    # ``os.linesep`` (CRLF on Windows) while ``.gitattributes`` pins ``*.json`` to
    # LF, so the worktree copy would disagree with its own index entry.
    BASELINE.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--detail", action="store_true", help="print every frozen function")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--reason", default="", help="why a new/grown function is accepted")
    args = parser.parse_args(argv)

    baseline = _load_baseline()
    if baseline and baseline.get("threshold") != THRESHOLD:
        print(
            f"function-length ratchet: baseline was measured at {baseline.get('threshold')} lines but "
            f"THRESHOLD is {THRESHOLD}. Re-measure and update both in the same commit.",
            file=sys.stderr,
        )
        return 2
    if not baseline and not args.update_baseline:
        # Fail before parsing ~4.6k functions: a missing baseline is not a
        # measurement problem, and scanning first made the failure 3 s slow.
        print(f"missing baseline: {BASELINE}", file=sys.stderr)
        print("run: python scripts/function_length_ratchet.py --update-baseline", file=sys.stderr)
        return 2

    current = collect()
    if args.detail:
        for key, length in current.items():
            frozen = (baseline.get("functions") or {}).get(key)
            print(f"  {length:4d}  (base {frozen if frozen is not None else 'new':>4})  {key}")

    failures, tightened = evaluate(current, baseline)
    if args.update_baseline:
        if failures and not args.reason.strip() and baseline:
            print(
                "function-length ratchet: refusing to freeze growth without --reason.\n"
                "Split the function, or state why the longer body is the right call:\n" + "\n".join(describe(failures)),
                file=sys.stderr,
            )
            return 1
        reasons = dict(baseline.get("reasons") or {})
        for _kind, key, _allowed, _length in failures:
            reasons[key] = args.reason.strip()
        _write_baseline(current, reasons, THRESHOLD)
        delta = len(current) - len(baseline.get("functions") or {})
        print(
            f"baseline updated: {len(baseline.get('functions') or {})} -> {len(current)} frozen function(s) "
            f"({delta:+d}), threshold {THRESHOLD}"
        )
        return 0

    if failures:
        print("function-length ratchet FAILED (split the function, or re-measure deliberately):", file=sys.stderr)
        print("\n".join(describe(failures)), file=sys.stderr)
        return 1

    print(
        f"function-length ratchet OK ({len(current)} frozen function(s) at >= {THRESHOLD} lines; "
        f"{len(tightened)} could be tightened)"
    )
    if tightened and args.detail:
        print("\n".join(tightened))
    return 0


if __name__ == "__main__":
    sys.exit(main())
