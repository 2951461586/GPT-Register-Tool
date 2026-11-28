"""``docs/audits/`` content index must stay complete and honest."""

from __future__ import annotations

import re
from pathlib import Path

_AUDITS = Path(__file__).resolve().parent.parent.parent / "docs" / "audits"
_INDEX = _AUDITS / "README.md"
_EXEMPT_FILES: frozenset[str] = frozenset({"README.md"})
_BARE_FILENAME = re.compile(r"`([A-Za-z0-9_.\-]+\.(?:md|txt))`")


def audit_files_on_disk() -> set[str]:
    """Every indexable file under ``docs/audits/`` (including its kind subdirectories)."""
    return {p.name for p in _AUDITS.rglob("*") if p.is_file() and p.suffix in {".md", ".txt"}} - _EXEMPT_FILES


def filenames_mentioned_in_index() -> set[str]:
    """Bare ``*.md`` / ``*.txt`` names listed in the **Contents** section."""
    text = _INDEX.read_text(encoding="utf-8")
    marker = "## Contents"
    if marker not in text:
        return set()
    return set(_BARE_FILENAME.findall(text.split(marker, 1)[1]))


def filenames_mentioned_anywhere() -> set[str]:
    """Every bare filename the README mentions, including prose."""
    return set(_BARE_FILENAME.findall(_INDEX.read_text(encoding="utf-8")))


def test_extractors_are_not_empty():
    assert len(audit_files_on_disk()) > 15, "extractor found no audit files -- the gate is checking nothing"
    assert len(filenames_mentioned_in_index()) > 15, "extractor found no referenced filenames -- the gate is blind"


def test_every_audit_file_is_listed_in_the_index():
    missing = sorted(audit_files_on_disk() - filenames_mentioned_in_index())
    assert missing == [], f"audit files missing from docs/audits/README.md: {missing}"


def test_index_never_references_a_file_that_does_not_exist():
    dangling = sorted(filenames_mentioned_anywhere() - audit_files_on_disk() - _EXEMPT_FILES)
    assert dangling == [], f"docs/audits/README.md references non-existent files: {dangling}"


def test_index_is_a_real_table_not_a_prose_dump():
    text = _INDEX.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if (line.startswith("| `") and ".md`" in line) or ".txt`" in line]
    assert len(rows) >= 15, "index does not look like a table -- expected one row per audit file"


def test_every_report_lives_in_the_directory_for_its_kind():
    kinds = {
        "audit-": "rounds",
        "scan": "scans",
        "plan-": "plans",
        "landing-": "landings",
    }
    misplaced = []
    for path in _AUDITS.rglob("*"):
        if not path.is_file() or path.name in _EXEMPT_FILES:
            continue
        expected = next((d for prefix, d in kinds.items() if path.name.startswith(prefix)), "records")
        if path.parent.name != expected:
            misplaced.append(f"{path.relative_to(_AUDITS)} (expected {expected}/)")
    assert misplaced == [], f"audit files in the wrong kind directory: {misplaced}"
