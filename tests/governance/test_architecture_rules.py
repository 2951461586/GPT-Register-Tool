"""The boundary rules live one-per-file and their numbers are a contract.

Why this exists
---------------
`docs/architecture.md` carried all 25 boundary rules in one table: a ~90-line
block whose longest rows (rule 19 in particular) grew every time a divergence was
recorded, next to an index that had to stay readable. Splitting the prose out is
only safe if the *numbers* survive: two dozen places in tests, scripts and other
docs cite `Rule 19` and friends, so a renumbering is a broken reference.

This module pins the split: files are `docs/rules/rule-NN-<slug>.md`, numbers are
contiguous from 01, the index lists every rule exactly once with a link that
resolves, each file still carries its check and source, and every `Rule N`
reference anywhere in the repository has a file behind it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = ROOT / "docs" / "architecture.md"
RULES_DIR = ROOT / "docs" / "rules"
RULE_FILE = re.compile(r"^rule-(\d{2})-[a-z0-9-]+\.md$")
INDEX_ROW = re.compile(r"^\| (\d+) \| .+ \| \[`(rule-\d{2}-[a-z0-9-]+\.md)`\]\(rules/(rule-\d{2}-[a-z0-9-]+\.md)\) \|$")


def rule_files() -> dict[int, Path]:
    found: dict[int, Path] = {}
    for path in sorted(RULES_DIR.glob("*.md")):
        match = RULE_FILE.match(path.name)
        assert match, f"unexpected file in docs/rules/: {path.name}"
        number = int(match.group(1))
        assert number not in found, f"duplicate rule number {number}: {path.name} and {found[number].name}"
        found[number] = path
    return found


def index_rows() -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line in ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        match = INDEX_ROW.match(line)
        if match:
            number, label, target = match.groups()
            assert label == target, f"index label and link disagree: {line}"
            rows.append((int(number), target))
    return rows


def test_numbers_are_contiguous_from_one():
    numbers = sorted(rule_files())
    assert numbers == list(range(1, len(numbers) + 1))


def test_the_index_lists_every_rule_exactly_once():
    files = rule_files()
    rows = index_rows()
    assert [number for number, _target in rows] == sorted(files), "the index and the files disagree"
    for number, target in rows:
        assert (RULES_DIR / target).is_file(), f"index links to a missing file: {target}"


def test_every_rule_file_carries_its_check_and_source():
    for number, path in rule_files().items():
        text = path.read_text(encoding="utf-8")
        assert text.startswith(f"# Rule {number:02d} — "), f"{path.name} has a mismatched heading"
        assert "- **Enforced by**: `" in text, f"{path.name} lost its check"
        assert "- **Source section**: " in text, f"{path.name} lost its source section"
        rule_body = text.split("## Rule", 1)[1].strip()
        assert len(rule_body) > 40, f"{path.name} has an empty rule body"


def test_every_rule_reference_resolves_to_a_file():
    """`Rule N` is cited from tests, scripts and docs; each number must exist."""
    files = rule_files()
    offenders: list[str] = []
    pattern = re.compile(r"Rule (\d{1,2})\b")
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".cs"}:
            continue
        parts = set(path.parts)
        if parts & {"bin", "obj", "runtime", "node_modules", ".git", "audits"}:
            continue
        if path == Path(__file__):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in pattern.finditer(text):
            number = int(match.group(1))
            if number not in files:
                offenders.append(f"{path.relative_to(ROOT)}: Rule {number}")
    assert offenders == [], "references to rules with no file: " + ", ".join(sorted(set(offenders)))
