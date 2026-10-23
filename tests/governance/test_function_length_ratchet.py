"""Tests for ``scripts/function_length_ratchet.py``.

The ratchet exists because function length was the one shape defect with no
gate: ``ruff`` does not measure it, and pi-lens only reports what a scan looked
at.  The measured baseline is **63** functions at or above 150 lines over
``sms_tool/``, ``services/`` and ``scripts/``.

These tests pin the properties that make the number mean something:

* the real tree is at baseline, so the gate is green rather than skipped;
* growth fails, and so does a brand-new long function -- proved by mutation,
  because a guard that cannot fail is not a guard;
* a shrink passes and is reported, so the baseline can be tightened;
* a missing baseline is a hard error, not a silent pass;
* adopting growth needs ``--reason``, so 214 lines are never frozen by accident;
* the threshold recorded in the baseline must match the script's constant --
  editing one without re-measuring the other must fail;
* keying is per function (``path::Qualname``), so a split in one function cannot
  pay for growth in another, and nested functions are judged on their own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import function_length_ratchet as ratchet  # noqa: E402

#: One real collection for the whole module.  ``collect()`` parses ~4.6k
#: functions; doing it once and driving the mutation tests with a slice of the
#: real result keeps them honest (real paths, real qualnames) without paying
#: 20 s of repeated parsing.
_REAL = ratchet.collect()


def _payload() -> dict:
    return json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))


def _scratch(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    return path


def _drive(monkeypatch, collected: dict) -> None:
    """Run ``main`` against a slice of the real collection."""
    monkeypatch.setattr(ratchet, "collect", lambda threshold=ratchet.THRESHOLD: dict(collected))


def _largest(payload: dict) -> str:
    return max(payload["functions"], key=lambda key: payload["functions"][key])


def _smallest(payload: dict) -> str:
    return min(payload["functions"], key=lambda key: payload["functions"][key])


def test_script_reports_ok_on_the_current_tree(capsys):
    """Through ``main`` (the subprocess adds a second 3 s parse for no coverage)."""
    assert ratchet.main([]) == 0
    out = capsys.readouterr().out
    assert "ratchet OK" in out
    assert "63 frozen function(s)" in out


def test_baseline_is_populated_and_self_consistent():
    payload = _payload()
    assert payload["threshold"] == ratchet.THRESHOLD
    assert payload["total"] == len(payload["functions"]) == 63
    assert all(length >= ratchet.THRESHOLD for length in payload["functions"].values())
    assert set(payload["functions"]) == set(_REAL), "the baseline no longer matches the tree"


def test_baseline_is_written_with_lf_endings():
    raw = ratchet.BASELINE.read_bytes()
    assert b"\r\n" not in raw, "baseline written with CRLF; .gitattributes pins eol=lf"
    assert raw.endswith(b"\n")


def test_growth_is_detected(tmp_path, monkeypatch):
    payload = _payload()
    victim = _largest(payload)
    payload["functions"][victim] -= 1
    monkeypatch.setattr(ratchet, "BASELINE", _scratch(tmp_path, payload))
    _drive(monkeypatch, {victim: _REAL[victim]})
    assert ratchet.main([]) == 1, "a function that grew by one line must fail the ratchet"


def test_a_new_long_function_is_detected(tmp_path, monkeypatch):
    """Deleting an entry makes the current tree look like it added one."""
    payload = _payload()
    victim = _smallest(payload)
    del payload["functions"][victim]
    monkeypatch.setattr(ratchet, "BASELINE", _scratch(tmp_path, payload))
    _drive(monkeypatch, {victim: _REAL[victim]})
    assert ratchet.main([]) == 1, "an unfrozen long function must fail the ratchet"


def test_a_shrink_passes_and_is_reported(tmp_path, monkeypatch, capsys):
    payload = _payload()
    victim = _smallest(payload)
    payload["functions"][victim] += 10
    monkeypatch.setattr(ratchet, "BASELINE", _scratch(tmp_path, payload))
    _drive(monkeypatch, {victim: _REAL[victim]})
    assert ratchet.main([]) == 0
    assert "1 could be tightened" in capsys.readouterr().out


def test_a_shrink_in_one_function_does_not_mask_growth_in_another(tmp_path, monkeypatch, capsys):
    """Per-function keys are the point: a total would hide this."""
    payload = _payload()
    shrunk, grown = _smallest(payload), _largest(payload)
    payload["functions"][shrunk] += 20
    payload["functions"][grown] -= 1
    monkeypatch.setattr(ratchet, "BASELINE", _scratch(tmp_path, payload))
    _drive(monkeypatch, {shrunk: _REAL[shrunk], grown: _REAL[grown]})
    assert ratchet.main([]) == 1
    assert grown in capsys.readouterr().err


def test_missing_baseline_is_a_hard_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ratchet, "BASELINE", tmp_path / "nope.json")
    assert ratchet.main([]) == 2


def test_detail_mode_lists_every_frozen_function(capsys):
    assert ratchet.main(["--detail"]) == 0
    out = capsys.readouterr().out
    assert "sms_tool/wallet_provider.py::run_wallet_provider" in out


def test_updating_a_shrink_needs_no_reason(tmp_path, monkeypatch):
    baseline = _scratch(
        tmp_path,
        {"threshold": ratchet.THRESHOLD, "total": 1, "functions": {"a.py::f": 200}, "reasons": {}},
    )
    monkeypatch.setattr(ratchet, "BASELINE", baseline)
    _drive(monkeypatch, {"a.py::f": 160})
    assert ratchet.main(["--update-baseline"]) == 0
    updated = json.loads(baseline.read_text(encoding="utf-8"))
    assert updated["functions"] == {"a.py::f": 160}


def test_adopting_growth_without_a_reason_is_refused(tmp_path, monkeypatch, capsys):
    baseline = _scratch(
        tmp_path,
        {"threshold": ratchet.THRESHOLD, "total": 1, "functions": {"a.py::f": 160}, "reasons": {}},
    )
    monkeypatch.setattr(ratchet, "BASELINE", baseline)
    _drive(monkeypatch, {"a.py::f": 190})
    assert ratchet.main(["--update-baseline"]) == 1
    assert "refusing" in capsys.readouterr().err
    assert json.loads(baseline.read_text(encoding="utf-8"))["functions"] == {"a.py::f": 160}


def test_adopting_growth_with_a_reason_records_it(tmp_path, monkeypatch):
    baseline = _scratch(
        tmp_path,
        {"threshold": ratchet.THRESHOLD, "total": 1, "functions": {"a.py::f": 160}, "reasons": {}},
    )
    monkeypatch.setattr(ratchet, "BASELINE", baseline)
    _drive(monkeypatch, {"a.py::f": 190})
    assert ratchet.main(["--update-baseline", "--reason", "protocol gate order is load-bearing"]) == 0
    updated = json.loads(baseline.read_text(encoding="utf-8"))
    assert updated["functions"] == {"a.py::f": 190}
    assert updated["reasons"] == {"a.py::f": "protocol gate order is load-bearing"}


def test_a_redefined_threshold_is_refused(tmp_path, monkeypatch):
    """Editing ``THRESHOLD`` without re-measuring must not silently re-baseline."""
    payload = _payload()
    payload["threshold"] = ratchet.THRESHOLD + 50
    monkeypatch.setattr(ratchet, "BASELINE", _scratch(tmp_path, payload))
    assert ratchet.main([]) == 2


def test_qualnames_distinguish_methods_and_nested_functions(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text(
        "class A:\n    def m(self):\n        def inner():\n            pass\n        pass\n\n\ndef top():\n    pass\n",
        encoding="utf-8",
    )
    found = ratchet.functions_in(source, threshold=1)
    assert set(found) == {"A.m", "A.m.<locals>.inner", "top"}, found


def test_a_long_nested_function_is_judged_separately(tmp_path):
    source = tmp_path / "sample.py"
    body = "\n".join(f"        x{i} = {i}" for i in range(10))
    source.write_text(f"def outer():\n    def inner():\n{body}\n    pass\n", encoding="utf-8")
    found = ratchet.functions_in(source, threshold=8)
    assert set(found) == {"outer", "outer.<locals>.inner"}, found
    assert found["outer"] > found["outer.<locals>.inner"]
