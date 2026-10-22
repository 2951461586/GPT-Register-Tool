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

Where the claims live
---------------------
``scripts/directory_map_groups.json``: one entry per group, each a list of
``git ls-files`` pathspecs. The claims used to be a ``git ls-files ...`` command
inside every table row, which meant the doc carried a 41-line copy of the
inventory that drifted from the pathspecs it was supposed to describe. The doc
now keeps only what a human reads -- the group, its files in prose, and the
boundary -- and the machine-readable claims live in one file next to this gate.

The two halves are pinned together: a group in the JSON with no row in the doc
(or a row with no JSON entry) fails, so the table cannot silently grow a
category the gate never measures.

What it does
------------
Resolves each group's pathspecs through ``git ls-files``, intersects the result
with the tracked ``sms_tool/**/*.py`` set, and fails when a tracked module is
claimed by no group -- or when the doc and the JSON disagree about the groups.

Only **tracked** files count. An untracked scratch module is not yet part of
the source inventory, so it neither enters the denominator nor can it hide one.

Duplicates (a path claimed by more than one group) are reported but do not fail:
overlapping workflow/implementation rows are legitimate, and the existing
cross-row claims predate this gate.

Usage:
    python scripts/module_coverage_check.py            # check
    python scripts/module_coverage_check.py --detail   # show every claim
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY_MAP = ROOT / "docs" / "directory-map.md"
GROUPS_CONFIG = ROOT / "scripts" / "directory_map_groups.json"

SECTION_START = "## `sms_tool/` module groups"
SECTION_END = "### Module-group coverage"

#: Header/filler rows of the Markdown table, not group names.
_HEADER_CELLS = {"Group", "Files", "Boundary", "Check (recompute)"}


def tracked_paths(*pathspecs: str) -> set[str]:
    """Tracked paths matching ``pathspecs``, POSIX-relative to the repo root."""
    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=str(ROOT),
    )
    return {path.decode() for path in raw.split(b"\0") if path}


def load_groups(path: Path | None = None) -> dict[str, list[str]]:
    """Load ``group -> pathspecs`` from the JSON config, validating the shape."""
    source = path if path is not None else GROUPS_CONFIG
    data = json.loads(source.read_text(encoding="utf-8"))
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, dict) or not groups:
        raise ValueError(f"{source.name}: expected a non-empty 'groups' object")
    out: dict[str, list[str]] = {}
    for name, pathspecs in groups.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{source.name}: group names must be non-empty strings")
        if not isinstance(pathspecs, list) or not pathspecs:
            raise ValueError(f"{source.name}: group {name!r} must list at least one pathspec")
        if not all(isinstance(item, str) and item.strip() for item in pathspecs):
            raise ValueError(f"{source.name}: group {name!r} has a non-string pathspec")
        out[name] = list(pathspecs)
    return out


def doc_groups(text: str) -> list[str]:
    """Group names as written in the module-group table, in document order."""
    section = text.split(SECTION_START, 1)[1].split(SECTION_END, 1)[0]
    names: list[str] = []
    for row in section.splitlines():
        if not row.startswith("| "):
            continue
        first_cell = row.split("|")[1].strip()
        if not first_cell or first_cell in _HEADER_CELLS or set(first_cell) <= {"-", ":", " "}:
            continue
        names.append(first_cell)
    return names


def parse_claims(groups: dict[str, list[str]], resolver=tracked_paths) -> tuple[dict[str, list[str]], int]:
    """Map each claimed path to its owning group names.

    Returns ``(claims, groups_resolved)``. ``resolver`` is the ``git ls-files``
    lookup and is injectable for tests, so the complement check can be proven
    without a real Git index.
    """
    claims: dict[str, list[str]] = collections.defaultdict(list)
    for group, pathspecs in groups.items():
        for path in resolver(*pathspecs):
            claims[path].append(group)
    return dict(claims), len(groups)


def check(
    text: str | None = None,
    tracked: set[str] | None = None,
    resolver=tracked_paths,
    groups: dict[str, list[str]] | None = None,
) -> tuple[list[str], dict[str, list[str]], int, list[str]]:
    """Return ``(unclaimed, claims, groups_resolved, group_mismatches)``.

    ``group_mismatches`` is the symmetric difference between the doc's table and
    the JSON config, so a category cannot exist in only one of the two.
    """
    source = text if text is not None else DIRECTORY_MAP.read_text(encoding="utf-8")
    group_map = groups if groups is not None else load_groups()
    if tracked is None:
        tracked = {path for path in tracked_paths("sms_tool") if path.endswith(".py")}
    claims, groups_resolved = parse_claims(group_map, resolver)
    claimed = tracked & set(claims)
    unclaimed = sorted(tracked - claimed)

    in_doc = doc_groups(source)
    mismatches = [f"only in docs/directory-map.md: {name}" for name in in_doc if name not in group_map]
    mismatches += [f"only in {GROUPS_CONFIG.name}: {name}" for name in group_map if name not in in_doc]
    return unclaimed, claims, groups_resolved, mismatches


def coverage_counts(tracked: set[str], claims: dict[str, list[str]]) -> tuple[set[str], dict[str, list[str]]]:
    """Report only tracked sms_tool modules, not incidental row-level paths."""
    named = tracked & claims.keys()
    duplicates = {path: claims[path] for path in sorted(named) if len(claims[path]) > 1}
    return named, duplicates


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--detail", action="store_true", help="print each claimed tracked module")
    args = ap.parse_args(argv)

    if not DIRECTORY_MAP.is_file():
        print(f"missing {DIRECTORY_MAP}")
        return 2
    if not GROUPS_CONFIG.is_file():
        print(f"missing {GROUPS_CONFIG}")
        return 2

    tracked = {path for path in tracked_paths("sms_tool") if path.endswith(".py")}
    unclaimed, claims, groups_resolved, mismatches = check(tracked=tracked)
    named, duplicates = coverage_counts(tracked, claims)

    if args.detail:
        for path in sorted(named):
            print(f"  {path}  <- {', '.join(claims[path])}")
        print(f"groups resolved: {groups_resolved}")

    if mismatches:
        print("module coverage FAILED: docs/directory-map.md and scripts/directory_map_groups.json disagree:")
        print("\n".join(f"  {line}" for line in mismatches))
        return 1

    if unclaimed:
        print("module coverage FAILED: tracked sms_tool modules claimed by no directory-map row:")
        print("\n".join(f"  {path}" for path in unclaimed))
        print(
            "hint: add the module to a group in scripts/directory_map_groups.json "
            "(and to its row's Files column in docs/directory-map.md)."
        )
        return 1

    print(
        f"module coverage OK ({len(tracked)} tracked sms_tool modules, "
        f"{len(named)} claimed, {len(unclaimed)} unclaimed, "
        f"{groups_resolved} groups, {len(duplicates)} cross-row claims)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
