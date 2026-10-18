"""Keep ``docs/current/cli-reference.md`` generated from the real parser.

A hand-maintained flag table drifts the moment a parser module changes. The
reference is generated, so this test reduces to "was it regenerated?".
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "generate_cli_reference.py"
REFERENCE = ROOT / "docs" / "current" / "cli-reference.md"


def test_reference_file_exists_and_is_indexed() -> None:
    assert REFERENCE.is_file()
    index = (ROOT / "docs" / "current" / "README.md").read_text(encoding="utf-8")
    assert "cli-reference.md" in index


def test_reference_matches_the_parser() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (result.stdout + result.stderr).strip()
