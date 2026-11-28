"""``scripts/pyright_ratchet.py`` — the type-error ratchet."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from scripts import pyright_ratchet as pr


def test_only_errors_are_counted_and_paths_are_repo_relative():
    payload = {
        "generalDiagnostics": [
            {"file": str(pr.ROOT / "sms_tool" / "a.py"), "severity": "error"},
            {"file": str(pr.ROOT / "sms_tool" / "a.py"), "severity": "error"},
            {"file": str(pr.ROOT / "sms_tool" / "b.py"), "severity": "warning"},
            {"file": str(pr.ROOT / "sms_tool" / "c.py"), "severity": "information"},
        ]
    }
    errors, warnings = pr.count_errors(payload)
    assert dict(errors) == {"sms_tool/a.py": 2}
    assert dict(warnings) == {"sms_tool/b.py": 1}


def test_empty_payload_yields_empty_counters():
    errors, warnings = pr.count_errors({"generalDiagnostics": []})
    assert dict(errors) == {}
    assert dict(warnings) == {}


def test_a_file_absent_from_the_baseline_has_zero_allowance():
    assert pr.grown(Counter({"sms_tool/new.py": 1}), {}) == [("sms_tool/new.py", 1, 0)]


def test_a_file_at_or_below_its_allowance_is_clean():
    errors = Counter({"sms_tool/a.py": 3, "sms_tool/b.py": 1})
    allowed = {"sms_tool/a.py": 3, "sms_tool/b.py": 5}
    assert pr.grown(errors, allowed) == []


def test_a_shrink_in_one_file_cannot_mask_growth_in_another():
    errors = Counter({"sms_tool/shrunk.py": 0, "sms_tool/grown.py": 2})
    allowed = {"sms_tool/shrunk.py": 10, "sms_tool/grown.py": 1}
    assert pr.grown(errors, allowed) == [("sms_tool/grown.py", 2, 1)]


def test_corrupt_baseline_loads_as_empty_so_every_file_grows(tmp_path: Path):
    path = tmp_path / "b.json"
    path.write_text("{not json", encoding="utf-8")
    assert pr.load_baseline(path) == {}


def test_missing_baseline_loads_as_empty(tmp_path: Path):
    assert pr.load_baseline(tmp_path / "nope.json") == {}


def test_non_int_entries_are_skipped_not_crashed():
    assert pr.baseline_per_file({"per_file": {"a.py": "x", "b.py": 2}}) == {"b.py": 2}


def test_run_pyright_raises_when_the_checker_produces_nothing(monkeypatch: pytest.MonkeyPatch):
    class _Result:
        stdout = ""
        stderr = "pyright not found"
        returncode = 127

    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(RuntimeError):
        pr.run_pyright()


def test_run_pyright_raises_on_unparseable_json(monkeypatch: pytest.MonkeyPatch):
    class _Result:
        stdout = "<html>not json</html>"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(RuntimeError):
        pr.run_pyright()


def test_baseline_exists_and_pins_a_total():
    assert pr.BASELINE.is_file(), f"missing baseline: {pr.BASELINE}"
    data = json.loads(pr.BASELINE.read_text(encoding="utf-8"))
    assert isinstance(data.get("total"), int)
    assert isinstance(data.get("per_file"), dict)
    assert data["total"] == sum(pr.baseline_per_file(data).values())


def test_script_reports_ok_on_the_current_tree():
    assert pr.main([]) == 0, "pyright regressed above the frozen baseline"
