"""Shared plumbing for the baseline ratchets: read, write, report.

Why this exists
---------------
This repository has 20+ ``*_ratchet.py`` / ``*_report.py`` gates, and each one
re-implemented the same four things: "load a JSON baseline", "write it back on
``--update-baseline``", "print a failure block to stderr and exit 1", and "sort
keys so a re-freeze produces a one-line diff". The rewrites drifted: some wrote
``\\r\\n`` on Windows, some dumped ``ensure_ascii=False`` and some
``ensure_ascii=True`` (so a Chinese file name in a key produced a different diff
depending on which gate re-froze it), and a few forgot the trailing newline.

What this module is NOT
-----------------------
It is not a framework. The gates' **rules and failure text are their value**, and
that text is pinned verbatim by ``tests/governance/test_*_ratchet.py``. A shared
runner that generated the messages would force 20 rewrites of pinned prose, which
is why ``docs/audits/scans/scan-2026-11-20-module-refactor-review.md`` downgraded
the original "600-900 line" estimate to "about 300 lines of plumbing".

Adoption is therefore incremental: new gates start here, and an existing gate
adopts :func:`read_json` / :func:`write_json` / :func:`print_failures` when it is
next touched for another reason -- never in a dedicated "refactor the gates"
change that would touch 20 pinned failure texts at once.

Adopted so far
--------------
``unittest_style_ratchet.py``. Two candidates were surveyed and deliberately not
converted in the same change: ``extractor_parity_report.py`` writes its baseline
with ``sort_keys=True`` (switching would reorder a curated file and bury the real
diff) and ``root_module_ratchet.py`` freezes a *set of names* with its own pinned
messages. Both adopt :func:`read_json` / :func:`write_json` when they are next
touched for another reason.

Standard library only, and import-safe from a test: no module-level file IO.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "add_update_flag",
    "print_failures",
    "read_json",
    "update_requested",
    "write_json",
]


def read_json(path: Path) -> dict[str, Any]:
    """Load a baseline object.

    ``utf-8-sig`` because several baselines were hand-edited in an editor that
    wrote a BOM; a plain ``utf-8`` read then fails inside ``json.loads`` with a
    message that does not name the file.
    """
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path.name}: baseline must be a JSON object, got {type(value).__name__}")
    return value


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a baseline deterministically: 2-space indent, LF, trailing newline.

    Key order is the caller's (baselines are ordered for review, not sorted):
    ``sort_keys`` would reorder a curated file on every re-freeze and hide the
    real change in the diff. ``ensure_ascii=False`` keeps Chinese group names
    readable, which is why every adopted gate produces the same bytes on any OS.
    """
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def add_update_flag(parser: argparse.ArgumentParser) -> None:
    """Add the one flag every ratchet spells the same way."""
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="re-freeze the baseline from the tree (review the diff; growth needs a decision)",
    )


def update_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "update_baseline", False))


def print_failures(header: str, problems: list[str], stream: Any = None) -> int:
    """Print ``header`` + one indented bullet per problem, and return the exit code.

    ASCII-only on purpose: CI runs on a Windows runner whose stdout is cp1252, so
    a non-ASCII bullet would raise ``UnicodeEncodeError`` inside the gate that was
    supposed to report the failure.
    """
    out = stream if stream is not None else sys.stderr
    print(header, file=out)
    for problem in problems:
        print(f"  - {problem}", file=out)
    return 1
