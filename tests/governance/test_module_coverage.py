"""Tests for ``scripts/module_coverage_check.py``.

``docs_consistency_scan.py`` only proves the forward direction -- every path a
doc points at exists. Nothing measured the complement: a module that exists but
no group claims. These tests pin the reverse-coverage gate:

* the real tree is fully claimed, so the gate is green rather than skipped;
* an unclaimed module **fails** -- proved by mutation, because a guard that
  cannot fail is not a guard;
* the claims come from ``scripts/directory_map_groups.json`` and the doc's group
  rows must agree with it in both directions, so neither half can grow a
  category the other never sees;
* duplicate claims are reported but do not fail.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import module_coverage_check as gate  # noqa: E402

SCRIPT = ROOT / "scripts" / "module_coverage_check.py"

_DOC = """\
# Directory Map

## `sms_tool/` module groups

| Group | Files | Boundary |
| --- | --- | --- |
| Alpha | `a.py` | first |
| Beta | `b.py` | second |

### Module-group coverage

Trailing prose that must be ignored.
"""

_GROUPS = {
    "Alpha": ["sms_tool/a.py"],
    "Beta": ["sms_tool/b.py", "sms_tool/c.py"],
}

_TRACKED = {"sms_tool/a.py", "sms_tool/b.py", "sms_tool/c.py"}


def _resolver(*pathspecs: str) -> set[str]:
    known = _TRACKED | {"sms_tool/zzz.py"}
    return {path for path in known if path in pathspecs}


def test_real_tree_is_fully_claimed() -> None:
    unclaimed, _, groups_resolved, mismatches = gate.check()
    assert unclaimed == []
    assert groups_resolved > 0
    assert mismatches == []


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
    unclaimed, claims, _, mismatches = gate.check(
        _DOC,
        tracked=_TRACKED | {"sms_tool/zzz.py"},
        resolver=_resolver,
        groups=_GROUPS,
    )
    assert unclaimed == ["sms_tool/zzz.py"]
    assert mismatches == []
    assert claims["sms_tool/b.py"] == ["Beta"]
    assert claims["sms_tool/c.py"] == ["Beta"]


def test_dropping_a_group_from_the_config_makes_its_modules_unclaimed() -> None:
    groups = {"Alpha": _GROUPS["Alpha"], "Beta": _GROUPS["Beta"]}
    unclaimed, _, _, _ = gate.check(_DOC, tracked=_TRACKED, resolver=_resolver, groups=groups)
    assert unclaimed == []

    without_beta = {"Alpha": _GROUPS["Alpha"]}
    unclaimed, _, _, mismatches = gate.check(_DOC, tracked=_TRACKED, resolver=_resolver, groups=without_beta)
    assert unclaimed == ["sms_tool/b.py", "sms_tool/c.py"]
    assert mismatches == ["only in docs/directory-map.md: Beta"]


def test_a_group_only_in_the_doc_is_a_mismatch() -> None:
    doc = _DOC.replace(
        "| Beta | `b.py` | second |\n",
        "| Beta | `b.py` | second |\n| Gamma | `c.py` | third |\n",
    )
    _, _, _, mismatches = gate.check(doc, tracked=_TRACKED, resolver=_resolver, groups=_GROUPS)
    assert mismatches == ["only in docs/directory-map.md: Gamma"]


def test_a_group_only_in_the_config_is_a_mismatch() -> None:
    groups = dict(_GROUPS, Gamma=["sms_tool/c.py"])
    _, _, _, mismatches = gate.check(_DOC, tracked=_TRACKED, resolver=_resolver, groups=groups)
    assert mismatches == [f"only in {gate.GROUPS_CONFIG.name}: Gamma"]


def test_header_and_separator_rows_are_not_groups() -> None:
    assert gate.doc_groups(_DOC) == ["Alpha", "Beta"]


def test_duplicate_claims_are_reported_not_fatal() -> None:
    groups = {
        "Alpha": ["sms_tool/a.py", "sms_tool/b.py"],
        "Beta": ["sms_tool/b.py"],
    }
    doc = _DOC.replace("| Alpha | `a.py` | first |", "| Alpha | `a.py`, `b.py` | first |")
    unclaimed, claims, _, mismatches = gate.check(
        doc, tracked={"sms_tool/a.py", "sms_tool/b.py"}, resolver=_resolver, groups=groups
    )
    assert unclaimed == []
    assert mismatches == []
    assert claims["sms_tool/b.py"] == ["Alpha", "Beta"]


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


def test_load_groups_rejects_a_malformed_config(tmp_path) -> None:
    bad = tmp_path / "groups.json"
    bad.write_text('{"groups": {"Alpha": []}}', encoding="utf-8")
    with pytest.raises(ValueError, match="at least one pathspec"):
        gate.load_groups(bad)

    bad.write_text('{"groups": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty 'groups'"):
        gate.load_groups(bad)


def test_detail_omits_incidental_row_paths(monkeypatch, capsys) -> None:
    tracked = {"sms_tool/a.py"}
    claims = {
        "sms_tool/a.py": ["Alpha"],
        "scripts/irrelevant.py": ["Alpha", "Beta"],
    }
    monkeypatch.setattr(gate, "tracked_paths", lambda root: tracked)
    monkeypatch.setattr(gate, "check", lambda *, tracked: ([], claims, 2, []))
    assert gate.main(["--detail"]) == 0
    output = capsys.readouterr().out
    assert "sms_tool/a.py  <- Alpha" in output
    assert "scripts/irrelevant.py" not in output
    assert "(1 tracked sms_tool modules, 1 claimed, 0 unclaimed, 2 groups, 0 cross-row claims)" in output
