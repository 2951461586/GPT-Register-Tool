"""Tests for scripts/config_key_ratchet.py.

Same two-half pattern as test_mailbox_private_import_ratchet.py: the predicate
is exercised in both directions on synthetic config (a key inside the accepted
set passes, an injected typo fails), then the real tree is scanned so the
frozen baseline is enforced on every push.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import config_key_ratchet as ratchet  # noqa: E402


def test_validator_keys_are_extraction_not_hardcoded():
    # The validator-derived set must be non-empty -- an empty set here would
    # make the baseline silently accept nothing and fail on the next run.
    known = ratchet._validator_keys()
    assert "at_stability_probe_count" in known["registration"]
    assert known["registration"], "validator key extraction went empty"


def test_baseline_covers_both_sections():
    baseline = json.loads(ratchet.BASELINE.read_text(encoding="utf-8"))
    assert set(baseline) >= {"registration", "email_registration"}
    assert baseline["registration"], "registration baseline is empty"
    assert baseline["email_registration"], "email_registration baseline is empty"


def test_real_tree_has_no_unknown_keys():
    current = ratchet.accepted()
    baseline = {k: set(v) for k, v in json.loads(ratchet.BASELINE.read_text(encoding="utf-8")).items()}
    for sec in ratchet.SECTIONS:
        extra = current[sec] - baseline.get(sec, set())
        assert not extra, f"{sec}: keys outside baseline: {sorted(extra)}"


def test_a_typo_key_is_rejected(tmp_path, monkeypatch):
    # Inject a misspelled key through a patched _section_keys (the config-file
    # read), leaving ROOT alone so _validator_keys still finds the real
    # config.py.  The typo must land in the accepted set yet stay outside the
    # frozen baseline -- the exact two conditions the ratchet's failure branch
    # checks.
    bogus = tmp_path / "runtime.json"
    bogus.write_text(json.dumps({"registration": {"at_stability_probe_conut": 1}}), encoding="utf-8")
    orig = ratchet._section_keys
    monkeypatch.setattr(
        ratchet,
        "_section_keys",
        lambda path: orig(bogus) if Path(path).name == "runtime.json" else orig(path),
    )
    current = ratchet.accepted()
    assert "at_stability_probe_conut" in current["registration"]
    baseline = {k: set(v) for k, v in json.loads(ratchet.BASELINE.read_text(encoding="utf-8")).items()}
    assert "at_stability_probe_conut" not in baseline.get("registration", set())
