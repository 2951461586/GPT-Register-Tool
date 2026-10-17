"""Ratchet: pyright error count must not grow.

Why this exists
---------------
The repo had no type checker in CI. ``ruff`` gates syntax and a narrow lint set;
nothing measured *types*. On 2026-10-17 a first full ``pyright`` run over
``sms_tool``, ``services``, ``scripts`` and ``tests`` reported **1,453 errors**.
Most are one of three shapes:

* ``reportOptionalMemberAccess`` — ``x.get(...)`` where ``x`` is inferred
  ``dict | None`` because the guard is written as
  ``x.get(k) if isinstance(x.get(k), dict) else {}``. Pyright evaluates the two
  ``x.get(k)`` calls independently, so the second one keeps its ``None`` branch.
  Rewriting it as ``raw: object = x.get(k)`` + ``raw if isinstance(raw, dict)``
  is behaviour-identical and removes the error.
* ``reportAttributeAccessIssue`` in ``tests/`` — ``unittest.mock`` replaces
  attributes at runtime (``mock.call_args``, ``mock.side_effect``) and Playwright
  page objects expose members the stubs do not model. ``pyrightconfig.json``
  turns those two rules off for ``tests/`` only.
* ``reportPossiblyUnboundVariable`` on ``for`` loop variables whose iterable the
  checker cannot prove non-empty. Initialising the variable before the loop is
  the fix.

Why a ratchet and not a big-bang cleanup
----------------------------------------
Deleting 1,453 errors in one commit means touching ~200 files whose behaviour
tests may not fully pin. That is the same failure the unused-import audit hit
(``scripts/unused_import_ratchet.py``): a mass edit that cannot be reviewed, then
a full revert. So the count is frozen per file and may only go **down**. New
errors are blocked immediately; existing debt is retired file by file, each with
its own tests.

Per-file, not one total, for the same reason every other ratchet here is
per-file: with a single number, cleaning one file masks a regression in another.

What it measures
----------------
``pyright --outputjson`` over the paths ``pyrightconfig.json`` includes, counting
``severity == "error"`` only. Warnings and informations are not gated — they are
reported by ``--detail`` so drift is visible without failing CI.

The baseline stores two views:

* ``per_file`` — the gated number. A file absent from the baseline has an
  allowance of 0, so a brand-new file must be clean.
* ``total`` — the sum, pinned separately so a baseline that somehow loses rows
  fails loudly instead of silently passing.

Usage
-----
::

    python scripts/pyright_ratchet.py                 # check vs baseline
    python scripts/pyright_ratchet.py --detail        # per-file table + warnings
    python scripts/pyright_ratchet.py --update-baseline

``--update-baseline`` is for **lowering** the count after a real cleanup. It
prints the per-file delta so a reviewer can see exactly what moved.

Standard library only (pyright itself is invoked as a subprocess).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "pyright_baseline.json"

#: Paths handed to pyright. ``pyrightconfig.json`` narrows this further; passing
#: them explicitly keeps the ratchet honest when the config changes.
TARGETS: tuple[str, ...] = ("sms_tool", "services", "scripts", "tests")


def _relpath(raw: str) -> str:
    """Normalise a pyright file path to a POSIX repo-relative path."""
    path = Path(raw)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def run_pyright(targets: tuple[str, ...] = TARGETS) -> dict:
    """Invoke pyright and return the parsed JSON payload.

    Raises ``RuntimeError`` with the captured stderr when pyright cannot run at
    all — a broken checker must never be mistaken for a clean tree.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", *targets],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    payload = (proc.stdout or "").strip()
    if not payload:
        raise RuntimeError(
            "pyright produced no JSON output; cannot judge the tree. "
            f"exit={proc.returncode} stderr={proc.stderr.strip()[:400]}"
        )
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"pyright JSON output is not parseable: {exc}") from exc
    if not isinstance(data, dict) or "generalDiagnostics" not in data:
        raise RuntimeError("pyright JSON payload is missing 'generalDiagnostics'")
    return data


def count_errors(data: dict) -> tuple[Counter[str], Counter[str]]:
    """Return ``(errors_per_file, warnings_per_file)`` as repo-relative paths."""
    errors: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    for diag in data.get("generalDiagnostics") or []:
        severity = diag.get("severity")
        rel = _relpath(str(diag.get("file") or ""))
        if severity == "error":
            errors[rel] += 1
        elif severity == "warning":
            warnings[rel] += 1
    return errors, warnings


def load_baseline(path: Path = BASELINE) -> dict:
    """Load the baseline, treating a corrupt file as empty (fail loudly)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def baseline_per_file(baseline: dict) -> dict[str, int]:
    raw = baseline.get("per_file")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return out


def grown(errors: Counter[str], allowed: dict[str, int]) -> list[tuple[str, int, int]]:
    """Return ``(file, current, allowed)`` for every file over its allowance."""
    out: list[tuple[str, int, int]] = []
    for path in sorted(set(errors) | set(allowed)):
        current = errors.get(path, 0)
        limit = allowed.get(path, 0)
        if current > limit:
            out.append((path, current, limit))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--detail", action="store_true", help="print the per-file table and warnings")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline (only after LOWERING the count, i.e. a real cleanup)",
    )
    args = parser.parse_args(argv)

    try:
        data = run_pyright()
    except RuntimeError as exc:
        print(f"pyright-ratchet: {exc}", file=sys.stderr)
        return 2

    errors, warnings = count_errors(data)
    total = sum(errors.values())

    if args.detail:
        print("=== errors per file ===")
        for path, count in errors.most_common():
            print(f"  {count:5}  {path}")
        print("\n=== warnings per file (not gated) ===")
        for path, count in warnings.most_common():
            print(f"  {count:5}  {path}")

    if args.update_baseline:
        previous = baseline_per_file(load_baseline())
        payload = {
            "total": total,
            "per_file": dict(sorted(errors.items())),
        }
        with BASELINE.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated: {BASELINE.name} ({total} error(s) over {len(errors)} file(s))")
        for path in sorted(set(previous) | set(errors)):
            before = previous.get(path, 0)
            after = errors.get(path, 0)
            if before != after:
                print(f"  {before:5} -> {after:<5} {path}")
        return 0

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}", file=sys.stderr)
        print("run: python scripts/pyright_ratchet.py --update-baseline", file=sys.stderr)
        return 2

    baseline = load_baseline(BASELINE)
    allowed = baseline_per_file(baseline)

    regressions = grown(errors, allowed)
    pinned_total = baseline.get("total")
    if isinstance(pinned_total, int) and total > pinned_total:
        regressions.append(("<total>", total, pinned_total))

    if regressions:
        for path, current, limit in regressions:
            print(
                f"{path}: {current} > baseline {limit} (+{current - limit}). "
                f"Fix the type error, or -- if the checker is wrong -- narrow the value "
                f"explicitly (``raw: object = x.get(k)`` then ``isinstance``) rather than "
                f"silencing the rule.",
                file=sys.stderr,
            )
        return 1

    print(
        f"pyright ratchet OK ({total} error(s) over {len(errors)} file(s), "
        f"baseline {pinned_total if isinstance(pinned_total, int) else 'n/a'}; "
        f"{sum(warnings.values())} warning(s) not gated)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
