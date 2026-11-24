"""The coverage floor is a gate, and it lives in exactly one place.

Why this exists
---------------
CI measured coverage with `--cov-report=term-missing` and never compared the
number, so 72% and 41% were both green. On 2026-11-20 the measured total was
`57721 statements / 16107 missing = 72%` and the gate became
`--cov-fail-under=70` (two points of headroom).

Two failure modes this file closes:

* the flag silently disappears from CI — coverage goes back to a printout and
  nothing fails, which is the state this test was written to end;
* the workflow and `docs/current/testing.md` drift apart — an operator reading
  the doc runs a command that asserts a different number than CI enforces.

`--cov-fail-under` cannot live in `pytest.ini`: the floor only applies when
coverage is collected, and a bare `pytest` (the documented local default,
serial and ordering-deterministic) must stay usable.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
DOC = ROOT / "docs" / "current" / "testing.md"
PYTEST_INI = ROOT / "pytest.ini"

#: The one number both files must agree on.
FLOOR = 70


def _ci_floor() -> int | None:
    match = re.search(r"--cov-fail-under=(\d+)", CI.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else None


def test_ci_carries_the_coverage_floor() -> None:
    assert _ci_floor() == FLOOR, (
        "the CI test step must pass --cov-fail-under="
        f"{FLOOR}; found {_ci_floor()}. Coverage without a floor is a printout."
    )


def test_ci_still_collects_the_same_sources() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "--cov=sms_tool" in text
    assert "--cov=services" not in text


def test_the_documented_floor_matches_ci() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert f"--cov-fail-under={FLOOR}" in doc, (
        "docs/current/testing.md must show the same floor CI enforces, or an operator's "
        "reproduction command asserts a different number"
    )


def test_the_doc_records_the_measurement_the_floor_came_from() -> None:
    """A floor without its measurement is a guess nobody can re-derive."""
    doc = DOC.read_text(encoding="utf-8")
    assert "57721" in doc and "16107" in doc and "72%" in doc


def test_pytest_ini_does_not_carry_the_floor() -> None:
    """A bare `pytest` must stay usable: no coverage is collected there."""
    text = PYTEST_INI.read_text(encoding="utf-8")
    assert "cov-fail-under" not in text
    assert "--cov" not in text
