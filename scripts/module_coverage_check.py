"""Reverse-coverage gate for ``docs/directory-map.md`` module groups.

Why this exists
---------------
``scripts/docs_consistency_scan.py`` checks documentation in **one direction
only**: every path a doc points at must exist. It cannot see the opposite
defect -- a module that exists but that no doc row claims. Before this gate,
adding ``sms_tool/foo.py`` triggered **no check at all**; the module-group
coverage table in ``docs/directory-map.md`` was a hand-run snippet, so the only
thing keeping the inventory honest was an operator remembering to edit the doc.

That is the same defect class as the unused-import audit: the inventory looked
complete because nothing measured the complement. This script measures it.

What it does
------------
Parses every row in the ``## `sms_tool/` module groups`` table that carries a
``git ls-files ...`` command, intersects the claimed paths with the tracked
``sms_tool/**/*.py`` set, and fails when a tracked module is claimed by no row.

Only **tracked** files count. An untracked scratch module is not yet part of
the source inventory, so it neither enters the denominator nor can it hide one.

Duplicates (a path claimed by more than one row) are reported but do not fail:
overlapping workflow/implementation rows are legitimate, and the existing
cross-row claims predate this gate.

Usage:
    python scripts/module_coverage_check.py            # check
    python scripts/module_coverage_check.py --detail   # show every claim
"""

from __future__ import annotations

import argparse
import collections
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY_MAP = ROOT / "docs" / "directory-map.md"

SECTION_START = "## `sms_tool/` module groups"
SECTION_END = "### Module-group coverage"

_ROW = re.compile(r"^[|] (.+?) [|] ")
_COMMAND = re.compile(r"`(git ls-files [^`]+)`")


def tracked_paths(*pathspecs: str) -> set[str]:
    """Tracked paths matching ``pathspecs``, POSIX-relative to the repo root."""
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=str(ROOT),
    )
    return {path.decode() for path in raw.split(b"\0") if path}


def parse_claims(
    text: str, resolver=tracked_paths
) -> tuple[dict[str, list[str]], int]:
    """Map each claimed path to its owning group names.

    Returns ``(claims, rows_seen)``. Only rows that name a ``git ls-files``
    command are considered; prose rows are ignored. ``resolver`` is the
    ``git ls-files``-backed path lookup and is injectable for tests.
    """
    section = text.split(SECTION_START, 1)[1].split(SECTION_END, 1)[0]
    claims: dict[str, list[str]] = collections.defaultdict(list)
    rows_seen = 0
    for row in section.splitlines():
        if not row.startswith("| ") or "`git ls-files " not in row:
            continue
        group_match = _ROW.match(row)
        command_match = _COMMAND.search(row)
        if not group_match or not command_match:
            continue
        rows_seen += 1
        group = group_match.group(1).strip()
        argv = shlex.split(command_match.group(1))
        for path in resolver(*argv[2:]):
            claims[path].append(group)
    return dict(claims), rows_seen


def check(
    text: str | None = None, tracked: set[str] | None = None, resolver=tracked_paths
) -> tuple[list[str], dict[str, list[str]], int]:
    """Return ``(unclaimed, claims, rows_seen)`` for the tracked sms_tool modules.

    ``tracked`` and ``resolver`` are injectable so tests can prove the
    complement check without a real Git index.
    """
    source = text if text is not None else DIRECTORY_MAP.read_text(encoding="utf-8")
    if tracked is None:
        tracked = {path for path in tracked_paths("sms_tool") if path.endswith(".py")}
    claims, rows_seen = parse_claims(source, resolver)
    claimed = tracked & set(claims)
    unclaimed = sorted(tracked - claimed)
    return unclaimed, claims, rows_seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--detail", action="store_true", help="print every claimed path")
    args = ap.parse_args(argv)

    if not DIRECTORY_MAP.is_file():
        print(f"missing {DIRECTORY_MAP}")
        return 2

    unclaimed, claims, rows_seen = check()
    duplicates = {
        path: groups for path, groups in sorted(claims.items()) if len(groups) > 1
    }

    if args.detail:
        for path in sorted(claims):
            print(f"  {path}  <- {', '.join(claims[path])}")
        print(f"rows parsed: {rows_seen}")

    if unclaimed:
        print("module coverage FAILED: tracked sms_tool modules claimed by no directory-map row:")
        print("\n".join(f"  {path}" for path in unclaimed))
        print(
            "hint: add the module to a module-group row in docs/directory-map.md "
            "(or place it in an existing row's `git ls-files` pathspec)."
        )
        return 1

    print(
        f"module coverage OK ({len(claims)} tracked modules, {rows_seen} rows, "
        f"{len(duplicates)} cross-row claims)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
