"""Pin ``docs/audits/README.md``'s self-reported count to the real directory.

Why this exists
---------------
The README opens its Contents section with "<N> files."  On 2026-09-22 that
read "31 files" while the directory held 39 -- and *no gate could see it*.
``docs_consistency_scan.py`` validates line-number pointers, symbol tables,
release pointers and ``sms_tool/providers/*.py`` path existence; it never
compares a document's self-reported entry count against the entries.

A count that no automation reads is a count that drifts.  This guard makes the
drift a test failure instead of a latent lie: adding or removing a report now
requires updating the number in the same commit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDITS = ROOT / "docs" / "audits"
README = AUDITS / "README.md"

COUNT_RE = re.compile(r"^(\d+) files\.", re.MULTILINE)


def _reports() -> list[Path]:
    """Every indexed artefact in the directory (the README indexes itself too)."""
    return sorted(
        p for p in AUDITS.iterdir() if p.is_file() and p.suffix in {".md", ".txt"}
    )


def test_readme_self_reported_count_matches_directory():
    text = README.read_text(encoding="utf-8")
    match = COUNT_RE.search(text)
    assert match, (
        "docs/audits/README.md must open its Contents section with '<N> files.' "
        "-- the count is what this guard pins."
    )
    stated = int(match.group(1))
    actual = len(_reports())
    assert stated == actual, (
        f"docs/audits/README.md says {stated} files but the directory holds {actual}. "
        "Update the count in the same commit that adds or removes a report."
    )


def test_readme_mentions_every_report():
    text = README.read_text(encoding="utf-8")
    missing = [p.name for p in _reports() if p.name != README.name and p.name not in text]
    assert not missing, (
        "these reports exist in docs/audits/ but are never mentioned in README.md, "
        f"so the index is incomplete: {missing}"
    )
