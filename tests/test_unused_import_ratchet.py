"""Tests for ``scripts/unused_import_ratchet.py``.

The ratchet exists because ``F401`` cannot be enabled as a *lint* rule on this
repo: 456 findings would have to be resolved one by one, and three measured
attempts to automate that (a static scanner over six-plus consumption channels)
each ended in a full revert.  A ratchet gets the value -- growth is blocked --
without taking the risk.

These tests pin the behaviour that makes the ratchet meaningful:

* the real tree is at baseline, so the gate is actually green rather than
  skipped;
* **growth fails** -- proved by mutation, because a guard that cannot fail is
  not a guard;
* a missing baseline is a hard error, not a silent pass;
* the baseline is LF-only (it is a generated artifact; see the 2026-09-22 N1
  defect where ``Path.write_text`` silently rewrote a repo file as CRLF);
* per-file baselines, so a reduction in one file cannot mask growth in another.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import unused_import_ratchet as ratchet  # noqa: E402

SCRIPT = ROOT / "scripts" / "unused_import_ratchet.py"


def _payload() -> dict:
    return json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))


def _scratch(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    return path


def test_script_reports_ok_on_the_current_tree():
    """The gate is wired and green -- not silently skipped."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ratchet OK" in result.stdout


def test_baseline_is_populated_and_self_consistent():
    payload = _payload()
    assert payload["total"] > 0
    assert payload["per_file"], "a baseline with no per-file detail cannot localise growth"
    assert sum(payload["per_file"].values()) == payload["total"], "total drifted from per_file"


def test_baseline_is_written_with_lf_endings():
    raw = ratchet.BASELINE.read_bytes()
    assert b"\r\n" not in raw, "baseline written with CRLF; .gitattributes pins eol=lf"
    assert raw.endswith(b"\n"), "baseline must end with a single LF"


def test_growth_is_detected(tmp_path, monkeypatch):
    """Mutation: lower one file's allowance and the ratchet must fail."""
    payload = _payload()
    victim = max(payload["per_file"], key=lambda p: payload["per_file"][p])
    assert payload["per_file"][victim] >= 1
    payload["per_file"][victim] -= 1

    monkeypatch.setattr(ratchet, "BASELINE", _scratch(tmp_path, payload))
    assert ratchet.main([]) == 1, "a file that grew must fail the ratchet"


def test_missing_baseline_is_a_hard_error(tmp_path, monkeypatch):
    """Exiting 0 with no baseline would turn the gate into decoration."""
    monkeypatch.setattr(ratchet, "BASELINE", tmp_path / "nope.json")
    assert ratchet.main([]) == 2


def test_a_shrinking_file_does_not_mask_growth(tmp_path, monkeypatch):
    """Per-file baselines are the point: a grand total would hide this.

    The fixture lowers one file's allowance (growth) while raising another's
    (slack), so the aggregate stays favourable and only a per-file check fires.
    """
    payload = _payload()
    grown = max(payload["per_file"], key=lambda p: payload["per_file"][p])
    shrunk = min(payload["per_file"], key=lambda p: payload["per_file"][p])
    if grown == shrunk:
        return
    payload["per_file"][grown] -= 1
    payload["per_file"][shrunk] += 5

    monkeypatch.setattr(ratchet, "BASELINE", _scratch(tmp_path, payload))
    assert ratchet.main([]) == 1, "growth must fail even when the aggregate looks fine"


def test_detail_mode_lists_per_file_counts(capsys, monkeypatch):
    monkeypatch.setattr(ratchet, "BASELINE", ratchet.BASELINE)
    assert ratchet.main(["--detail"]) == 0
    out = capsys.readouterr().out
    assert "sms_tool/paypal_link/gen_link.py" in out, "detail output should name the worst file"
