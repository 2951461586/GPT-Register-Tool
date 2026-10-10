"""Pins the commit-gate list against ``.githooks/pre-commit``.

``.githooks/pre-commit`` is the authority -- it is what actually runs. The
``.githooks/`` row in ``docs/directory-map.md`` is the human-readable index of
it, and the two had already drifted once: the row still read "runs four gates"
after ``scripts/format_guard.py`` became the fifth (2026-10-08), because nothing
compared them. ``docs_consistency_scan.py`` cannot see this -- its weak tier
resolves ``path.py:NNN`` pointers only, and a gate list is neither a line
pointer nor a symbol table.

The comparison is bidirectional and exact. Both directions matter: an
undocumented gate is invisible to a reader deciding what will block a commit,
and a documented-but-absent gate promises protection that does not exist.

This module also records each gate's *pipeline coverage* -- see
``HOOK_GATES_WITHOUT_PIPELINE_COVERAGE``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks" / "pre-commit"
DIRECTORY_MAP = ROOT / "docs" / "directory-map.md"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TESTS = ROOT / "tests"

#: A hook invocation line looks like: ``"$PYTHON_BIN" scripts/format_guard.py || exit 1``.
_HOOK_INVOCATION = re.compile(r'^\s*"\$PYTHON_BIN"\s+scripts/([A-Za-z0-9_./-]+\.py)\b', re.M)

#: The responsibility cell of the ``.githooks/`` row names each gate in backticks.
_ROW_PREFIX = "| `.githooks/` |"
_BACKTICKED_PY = re.compile(r"`([A-Za-z0-9_./-]+\.py)`")

#: Hook gates enforced **only** by the local pre-commit hook: not invoked by
#: ``.github/workflows/ci.yml`` and not exercised by any test. The hook is
#: bypassable (``git commit --no-verify``) and is absent from a checkout that
#: never ran ``scripts/install_git_hooks.py``, so an entry here is a real hole,
#: not a formality.
#:
#: **The set is empty as of 2026-10-08**, and that is the point: it records that
#: every hook gate is now also enforced where it cannot be skipped. The last
#: entry was ``sentinel_asset_guard.py``, which had run only in the hook since it
#: was added -- and it exists because ``sentinel-runner.js`` was rewritten
#: uniformly CRLF->LF, the raw digest moved, every Sentinel issue call raised,
#: issuance silently degraded to the legacy issuer, and 126/126 accounts failed
#: (``docs/audits/scan-2026-09-18-sentinel-runner-hash-mismatch.md``). A gate
#: that only guards the accident that motivated it, in the one place a
#: contributor can skip, is not guarding it. ``ci.yml`` now runs it.
#:
#: The check is exact in **both** directions, which is what keeps this record
#: honest: a new hook gate without CI or test coverage fails, and so does
#: closing a hole without deleting its entry.
HOOK_GATES_WITHOUT_PIPELINE_COVERAGE: frozenset[str] = frozenset()


def _hook_gates() -> list[str]:
    """Return the gate scripts ``.githooks/pre-commit`` invokes, in order."""
    return _HOOK_INVOCATION.findall(HOOK.read_text(encoding="utf-8"))


def _documented_gates() -> list[str]:
    """Return the gate scripts named by the ``.githooks/`` row, in order."""
    for line in DIRECTORY_MAP.read_text(encoding="utf-8").splitlines():
        if not line.startswith(_ROW_PREFIX):
            continue
        cells = line.split("|")
        # ['', ' `.githooks/` ', ' Commit gates ', ' <responsibility> ', ' <notes> ', '']
        responsibility = cells[3]
        assert "gates" in responsibility, f"the `.githooks/` row no longer describes gates: {responsibility!r}"
        return _BACKTICKED_PY.findall(responsibility)
    raise AssertionError("docs/directory-map.md has no `.githooks/` row")


def _has_pipeline_coverage(gate: str) -> bool:
    """True when CI invokes ``gate`` or a test module actually loads it.

    The predicate deliberately requires a **path** reference to
    ``scripts/<gate>``, not a bare name match. A bare name match is
    self-satisfying: this module names every gate it records, so it would
    report its own record as coverage -- the same tautology the repo's audits
    flag (``scan-2026-09-04`` on counting hits without reading the hit line).
    The three real guard tests all use the ``ROOT / "scripts" / "<name>.py"``
    idiom, so requiring it costs nothing and cannot be satisfied by prose.
    """
    if gate in CI_WORKFLOW.read_text(encoding="utf-8"):
        return True
    stem = Path(gate).stem
    alternatives = [
        r'["\']scripts["\']\s*/\s*["\']' + re.escape(gate) + r'["\']',  # ROOT / "scripts" / "<gate>"
        r"scripts/" + re.escape(gate),  # "scripts/<gate>"
        r'spec_from_file_location\(\s*["\']' + re.escape(stem) + r'["\']',
    ]
    idiom = re.compile("|".join(alternatives))
    self_path = Path(__file__).resolve()
    return any(
        idiom.search(path.read_text(encoding="utf-8", errors="replace"))
        for path in TESTS.rglob("test_*.py")
        if path.resolve() != self_path
    )


def test_the_hook_parser_finds_real_gates() -> None:
    """Self-test the classifier before trusting its verdict.

    A weakened ``_HOOK_INVOCATION`` would silently report an empty gate list,
    and ``set() == set()`` would then pass every comparison below.
    """
    gates = _hook_gates()
    assert gates, "parsed no gates from .githooks/pre-commit -- the pattern is stale"
    for gate in gates:
        assert (ROOT / "scripts" / gate).is_file(), f"hook invokes a missing script: scripts/{gate}"


def test_documented_gate_set_equals_the_hook_set() -> None:
    """Bidirectional, exact: neither side may list a gate the other does not."""
    hook = set(_hook_gates())
    documented = set(_documented_gates())
    assert documented == hook, (
        "docs/directory-map.md `.githooks/` row and .githooks/pre-commit disagree.\n"
        f"  hook only (undocumented): {sorted(hook - documented)}\n"
        f"  doc only (not run):       {sorted(documented - hook)}"
    )


def test_documented_gate_order_matches_the_hook() -> None:
    """Order is load-bearing: the hook fails fast, so the first gate wins."""
    assert _documented_gates() == _hook_gates()


def test_the_uncovered_gate_set_is_exactly_what_we_recorded() -> None:
    """Pin which gates have no CI and no test coverage.

    Passes today only because ``sentinel_asset_guard.py`` is recorded above.
    Adding a gate without pipeline coverage, or closing this hole without
    updating the record, both fail on purpose.
    """
    uncovered = {gate for gate in _hook_gates() if not _has_pipeline_coverage(gate)}
    assert uncovered == set(HOOK_GATES_WITHOUT_PIPELINE_COVERAGE), (
        "hook-gate coverage changed.\n"
        f"  newly uncovered: {sorted(uncovered - set(HOOK_GATES_WITHOUT_PIPELINE_COVERAGE))}\n"
        f"  now covered:     {sorted(set(HOOK_GATES_WITHOUT_PIPELINE_COVERAGE) - uncovered)}"
    )
