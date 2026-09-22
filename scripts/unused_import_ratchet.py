"""Ratchet for unused imports (ruff ``F401``) -- freeze today's debt, block growth.

Why this exists
---------------
The 2026-09-22 four-axis audit found **456** unused imports across 84 files with
no automation able to see them: ``pyproject.toml`` selects
``["E9", "F63", "F7", "F82"]``, and ruff's ``select`` is an allow-list -- so
"ruff passes" only ever meant "the selected rules passed".  The finding had sat
there for 20 days because nothing was looking.

Adding ``F401`` to ``select`` was tried first and **abandoned after three
measured attempts**.  Clearing 456 findings requires deciding, per name, whether
it is a dead import or a re-export consumed somewhere -- and a static scanner
kept missing consumption channels.  It started with three, and the full test
suite exposed three more (``33 failed / 64 errors``), then more again
(``17 failed``).  The channels it kept discovering were:

1. ``from M import name``
2. ``from M import *``
3. ``import M`` + ``M.name``
4. ``from pkg import module as alias`` + ``alias.attr``
5. a dotted token inside a **string literal** (``patch("a.b.c")``, ratchet baselines)
6. ``patch.object(mod, "attr")`` / ``setattr(mod, "attr", ...)`` second argument
7. the **middle** segment of a dotted string token (``"...subprocess.run"`` consumes
   ``...subprocess``)
8. the **alias** side of ``from x import name as alias``

Those are not enumerable by inspection, and every miss costs a full revert of a
456-site change.  So the gate here is a **ratchet, not a clean-up**.

What it does
------------
Freezes the current per-file count and fails if any file grows.  New code must
not add unused imports.  A file that *shrinks* is fine -- lower the baseline
when you want to lock the gain in:

    python scripts/unused_import_ratchet.py --update-baseline

Per-file (not one grand total) so that a reduction in one file cannot mask
growth in another -- the same property ``mailbox_private_import_ratchet.py``
documents, for the same reason.

Usage:
    python scripts/unused_import_ratchet.py                  # check
    python scripts/unused_import_ratchet.py --detail         # per-file counts
    python scripts/unused_import_ratchet.py --update-baseline
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "unused_import_baseline.json"
SCAN_DIRS = ("sms_tool", "services", "scripts", "tests")


def collect() -> dict[str, int]:
    """Per-file ``F401`` counts, keyed by POSIX path relative to the repo root."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "F401",
            "--output-format",
            "json",
            *SCAN_DIRS,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )
    # ruff exits 0 (clean) or 1 (findings).  Anything else means ruff itself
    # failed -- treating that as "0 findings" would be a silent green.
    if proc.returncode not in (0, 1):
        raise SystemExit(
            f"ruff did not run (exit {proc.returncode}); the ratchet cannot measure:\n"
            f"{(proc.stderr or proc.stdout)[:2000]}"
        )
    counts: collections.Counter[str] = collections.Counter()
    for item in json.loads(proc.stdout or "[]"):
        rel = Path(item["filename"]).resolve().relative_to(ROOT).as_posix()
        counts[rel] += 1
    return dict(sorted(counts.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--detail", action="store_true", help="print per-file counts")
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args(argv)

    current = collect()

    if args.update_baseline:
        # 🔴 ``newline="\n"`` is mandatory, not cosmetic: ``Path.write_text``
        # defaults to ``newline=None``, which translates every ``\n`` to
        # ``os.linesep`` -- CRLF on Windows.  ``.gitattributes`` pins
        # ``*.json text eol=lf``, so a CRLF worktree copy would disagree with
        # its LF index entry while ``git diff`` still reported it clean.
        BASELINE.write_text(
            json.dumps(
                {"total": sum(current.values()), "per_file": current},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(
            f"baseline updated: {sum(current.values())} unused imports "
            f"across {len(current)} files"
        )
        return 0

    if not BASELINE.exists():
        print(f"missing baseline: {BASELINE}")
        print("run: python scripts/unused_import_ratchet.py --update-baseline")
        return 2

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    allowed: dict[str, int] = baseline.get("per_file", {})

    if args.detail:
        for path, n in sorted(current.items()):
            print(f"  {n:4d}  (base {allowed.get(path, 0):4d})  {path}")

    failures = [
        f"  {path}: {allowed.get(path, 0)} -> {n}"
        for path, n in sorted(current.items())
        if n > allowed.get(path, 0)
    ]
    if failures:
        print("unused-import ratchet FAILED (a file grew; remove the unused import):")
        print("\n".join(failures))
        return 1

    total = sum(current.values())
    print(
        f"unused-import ratchet OK ({total} <= baseline {baseline.get('total')}, "
        f"{len(current)} files)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
