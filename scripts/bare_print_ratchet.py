"""Ratchet for bare ``print()`` calls in sms_tool -- freezes the split output.

Why this exists
---------------
Operator-facing lines in the protocol lane go through bare ``print()`` while
diagnostic events go through ``logging``.  ``SanitizingTextIO`` +
``StdoutMirror`` already sanitise and mirror every printed line to
``backend_stdout.jsonl``, so existing prints are *functional* -- the debt is
that the two channels are not fed from one source and nothing stops the count
from growing.

This ratchet records the per-file bare-print count and fails if any file's
count grows.  New operator-facing lines should go through
``sms_tool.operator_output.emit`` (one call feeds stdout AND the log).  Lower
a baseline only after *removing* prints:

    python scripts/bare_print_ratchet.py --update-baseline

A call is "bare" when the callee name is exactly ``print`` -- ``safe_print``
(already the sanitising seam) and ``operator_output.emit`` are not counted.
Stdlib only; runs as a pytest test and as a git hook.

Usage:
    python scripts/bare_print_ratchet.py                  # check
    python scripts/bare_print_ratchet.py --detail         # per-file counts
    python scripts/bare_print_ratchet.py --update-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "bare_print_baseline.json"
SCAN_ROOT = "sms_tool"


def _bare_print_count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            count += 1
    return count


def scan() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted((ROOT / SCAN_ROOT).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        n = _bare_print_count(path)
        if n:
            counts[str(path.relative_to(ROOT)).replace("\\", "/")] = n
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args()

    current = scan()
    if args.update_baseline:
        # 🔴 ``newline="\n"`` is mandatory, not cosmetic: ``Path.write_text``
        # defaults to ``newline=None``, which translates every ``\n`` to
        # ``os.linesep`` -- CRLF on Windows.  ``.gitattributes`` pins
        # ``*.json text eol=lf``, so a CRLF worktree copy would disagree with
        # its LF index entry while ``git diff`` still reported it clean (both
        # sides get normalised before comparison).  That is exactly how this
        # baseline drifted out of sync on 2026-09-21; see
        # ``tests/test_line_ending_guard.py``.
        BASELINE.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"baseline updated: {sum(current.values())} bare prints across {len(current)} files")
        return 0

    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    failures = []
    for path, n in sorted(current.items()):
        base = baseline.get(path, 0)
        if n > base:
            failures.append(f"  {path}: {base} -> {n}")
    if args.detail:
        for path, n in sorted(current.items()):
            print(f"  {n:4d}  {path}")
    if failures:
        print("bare-print ratchet FAILED (a file grew; use operator_output.emit for new lines):")
        print("\n".join(failures))
        return 1
    print(f"bare-print ratchet OK ({sum(current.values())} bare prints, baseline frozen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
