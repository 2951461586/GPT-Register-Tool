"""``docs/audits/`` content index must stay complete and honest.

The round-3 audit flagged (P2 #17) that ``docs/audits/README.md`` was a 24-line
naming-convention note with **no content index at all** -- 22 audit files, none
listed. The index was added on 2026-09-09; this module keeps it from rotting.

Two directions, both required:

1. Every audit file on disk must appear in the README (otherwise new audits
   silently go unlisted -- exactly the failure the audit reported).
2. Every bare ``*.md`` / ``*.txt`` filename the README mentions must exist on
   disk (otherwise the index rots into dangling references).

Both extractors are asserted non-empty: an empty extraction turns every
set-difference assertion into a tautology and the gate goes green while
checking nothing.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_AUDITS = (
    Path(__file__).resolve().parent.parent / "docs" / "audits"
)
_INDEX = _AUDITS / "README.md"

# Files that legitimately live in docs/audits/ without being indexed.
_EXEMPT_FILES: frozenset[str] = frozenset({"README.md"})

# ``.md`` or ``.txt`` wrapped in backticks, with no path separator -- i.e. a
# sibling in the same directory. Anything containing "/" is a repo-relative
# pointer (``docs/architecture.md``) and is out of scope for this check.
_BARE_FILENAME = re.compile(r"`([A-Za-z0-9_.\-]+\.(?:md|txt))`")


def audit_files_on_disk() -> set[str]:
    """Every indexable file currently sitting in ``docs/audits/``."""
    return {
        p.name
        for p in _AUDITS.iterdir()
        if p.is_file() and p.suffix in {".md", ".txt"}
    } - _EXEMPT_FILES


def filenames_mentioned_in_index() -> set[str]:
    """Bare ``*.md`` / ``*.txt`` names listed in the **Contents** section.

    Deliberately scoped to the index tables, not the whole file. An earlier
    revision scanned the entire README and was defeated by a real mutation:
    deleting a file's row from the index table still passed, because the same
    name also appeared in the "Naming conventions" blurb. Being *mentioned* is
    not the same as being *indexed* -- the gate has to check the latter.
    """
    text = _INDEX.read_text(encoding="utf-8")
    marker = "## Contents"
    if marker not in text:
        return set()
    return set(_BARE_FILENAME.findall(text.split(marker, 1)[1]))


def filenames_mentioned_anywhere() -> set[str]:
    """Every bare filename the README mentions, including prose.

    Used for the dangling-reference check: a name pointing at nothing is wrong
    wherever it appears.
    """
    return set(_BARE_FILENAME.findall(_INDEX.read_text(encoding="utf-8")))


class AuditIndexCompleteness(unittest.TestCase):
    def test_extractors_are_not_empty(self):
        """Guard against the empty-set tautology.

        If either extractor silently returns nothing, every difference
        assertion below degenerates and the gate goes green while checking
        nothing. This is the failure mode that makes scan gates useless.
        """
        self.assertGreater(
            len(audit_files_on_disk()),
            15,
            "extractor found no audit files -- the gate is checking nothing",
        )
        self.assertGreater(
            len(filenames_mentioned_in_index()),
            15,
            "extractor found no referenced filenames -- the gate is blind",
        )

    def test_every_audit_file_is_listed_in_the_index(self):
        missing = sorted(audit_files_on_disk() - filenames_mentioned_in_index())
        self.assertEqual(
            [],
            missing,
            f"audit files missing from docs/audits/README.md: {missing}",
        )

    def test_index_never_references_a_file_that_does_not_exist(self):
        dangling = sorted(
            filenames_mentioned_anywhere() - audit_files_on_disk() - _EXEMPT_FILES
        )
        self.assertEqual(
            [],
            dangling,
            f"docs/audits/README.md references non-existent files: {dangling}",
        )

    def test_index_is_a_real_table_not_a_prose_dump(self):
        """The index must be scannable: file names inside markdown table rows.

        A prose paragraph that happens to mention every name would satisfy the
        two set checks above while being useless as an index.
        """
        text = _INDEX.read_text(encoding="utf-8")
        rows = [
            line
            for line in text.splitlines()
            if line.startswith("| `") and ".md`" in line or ".txt`" in line
        ]
        self.assertGreaterEqual(
            len(rows),
            15,
            "index does not look like a table -- expected one row per audit file",
        )


if __name__ == "__main__":
    unittest.main()
