"""Tests for ``scripts/module_coverage_check.py``.

``docs_consistency_scan.py`` only proves the forward direction -- every path a
doc points at exists. Nothing measured the complement: a module that exists but
no doc row claims. These tests pin the reverse-coverage gate:

* the real tree is fully claimed, so the gate is green rather than skipped;
* an unclaimed module **fails** -- proved by mutation, because a guard that
  cannot fail is not a guard;
* the parser reads only ``git ls-files`` rows, not prose;
* duplicate claims are reported but do not fail.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import module_coverage_check as gate  # noqa: E402

SCRIPT = ROOT / "scripts" / "module_coverage_check.py"

_DOC = """\
# Directory Map

## `sms_tool/` module groups

| Group | Files | Boundary | Check |
| --- | --- | --- | --- |
| Alpha | `a.py` | first | `git ls-files sms_tool/a.py` |
| Beta | `b.py` | second | `git ls-files sms_tool/b.py sms_tool/c.py` |
| Prose row | prose | prose | prose |

### Module-group coverage (recompute)

Trailing prose that must be ignored.
"""


def _resolver(*pathspecs: str) -> set[str]:
    known = {
        "sms_tool/a.py",
        "sms_tool/b.py",
        "sms_tool/c.py",
        "sms_tool/zzz.py",
    }
    return {path for path in known if path in pathspecs}


def test_real_tree_is_fully_claimed() -> None:
    unclaimed, _, rows_seen = gate.check()
    assert unclaimed == []
    assert rows_seen > 0


def test_script_exits_zero_on_real_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "module coverage OK" in proc.stdout
    tracked = {path for path in gate.tracked_paths("sms_tool") if path.endswith(".py")}
    assert f"({len(tracked)} tracked sms_tool modules, {len(tracked)} claimed, 0 unclaimed," in proc.stdout


def test_unclaimed_module_fails() -> None:
    unclaimed, claims, _ = gate.check(
        _DOC, tracked={"sms_tool/a.py", "sms_tool/b.py", "sms_tool/c.py", "sms_tool/zzz.py"},
        resolver=_resolver,
    )
    assert unclaimed == ["sms_tool/zzz.py"]
    assert claims["sms_tool/b.py"] == ["Beta"]
    assert claims["sms_tool/c.py"] == ["Beta"]


def test_removing_a_row_makes_its_module_unclaimed() -> None:
    mutated = _DOC.replace(
        "| Beta | `b.py` | second | `git ls-files sms_tool/b.py sms_tool/c.py` |\n", ""
    )
    unclaimed, _, _ = gate.check(
        mutated,
        tracked={"sms_tool/a.py", "sms_tool/b.py", "sms_tool/c.py"},
        resolver=_resolver,
    )
    assert unclaimed == ["sms_tool/b.py", "sms_tool/c.py"]


def test_prose_rows_are_ignored() -> None:
    _, _, rows_seen = gate.check(
        _DOC, tracked={"sms_tool/a.py"}, resolver=_resolver
    )
    assert rows_seen == 2


def test_duplicate_claims_are_reported_not_fatal() -> None:
    doc = _DOC.replace(
        "| Alpha | `a.py` | first | `git ls-files sms_tool/a.py` |",
        "| Alpha | `a.py` | first | `git ls-files sms_tool/a.py sms_tool/b.py` |\n"
        "| Gamma | `b.py` | overlap | `git ls-files sms_tool/b.py` |",
    )
    unclaimed, claims, _ = gate.check(
        doc, tracked={"sms_tool/a.py", "sms_tool/b.py"}, resolver=_resolver
    )
    assert unclaimed == []
    assert {"Alpha", "Gamma"} <= set(claims["sms_tool/b.py"])


def test_check_supports_injected_tracked_set_without_git() -> None:
    # The resolver is injected, so no real Git index is consulted.
    unclaimed, _, _ = gate.check(
        _DOC, tracked={"sms_tool/a.py", "sms_tool/zzz.py"}, resolver=_resolver
    )
    assert unclaimed == ["sms_tool/zzz.py"]


def test_report_counts_only_tracked_sms_tool_modules() -> None:
    tracked = {"sms_tool/a.py", "sms_tool/b.py"}
    claims = {
        "sms_tool/a.py": ["Alpha"],
        "sms_tool/b.py": ["Beta", "Gamma"],
        "scripts/irrelevant.py": ["Alpha", "Beta"],
        "SmsWorkbench/irrelevant.cs": ["Alpha"],
    }
    named, duplicates = gate.coverage_counts(tracked, claims)
    assert named == {"sms_tool/a.py", "sms_tool/b.py"}
    assert duplicates == {"sms_tool/b.py": ["Beta", "Gamma"]}


def test_detail_omits_incidental_row_paths(monkeypatch, capsys) -> None:
    tracked = {"sms_tool/a.py"}
    claims = {
        "sms_tool/a.py": ["Alpha"],
        "scripts/irrelevant.py": ["Alpha", "Beta"],
    }
    monkeypatch.setattr(gate, "tracked_paths", lambda root: tracked)
    monkeypatch.setattr(gate, "check", lambda *, tracked: ([], claims, 2))
    assert gate.main(["--detail"]) == 0
    output = capsys.readouterr().out
    assert "sms_tool/a.py  <- Alpha" in output
    assert "scripts/irrelevant.py" not in output
    assert "(1 tracked sms_tool modules, 1 claimed, 0 unclaimed, 2 rows, 0 cross-row claims)" in output
