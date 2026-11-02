"""Tests for scripts/experiment_ratchet.py -- the registration toggle ledger.

Same two-half pattern as the other ratchets: each predicate is proved to *fail*
on a synthetic tree (a guard that cannot fail is not a guard), then the real tree
is checked so the committed ledger stays enforced on every push.

The sandbox is a whole miniature repo -- ``sms_tool/`` source, ``config.example.json``,
the ledger and the runbook -- because the gate cross-checks all four against each
other and testing it against the real tree only would leave three of them unpinned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import experiment_ratchet as ratchet  # noqa: E402

SOURCE = """
LANE_KEY = "known_flag"


def enabled(config):
    return registration_flag(config, "known_flag", False)


def lane(config):
    return registration_flag(config, LANE_KEY, False)


def dynamic(config, key):
    return registration_flag(config, key, False)
"""

ENTRY = {
    "kind": "ab",
    "id": "P9-9",
    "default": False,
    "status": "open",
    "doc": "runbook.md",
}


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "sms_tool").mkdir()
    (tmp_path / "sms_tool" / "fake.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "runbook.md").write_text(
        "| P9-9 | hypothesis | `registration.known_flag` | evidence |\n", encoding="utf-8"
    )
    (tmp_path / "config.example.json").write_text(json.dumps({"registration": {"known_flag": False}}), encoding="utf-8")
    _write_ledger(tmp_path, {"known_flag": dict(ENTRY)})
    monkeypatch.setattr(ratchet, "SOURCE_ROOTS", (tmp_path / "sms_tool",))
    monkeypatch.setattr(ratchet, "LEDGER", tmp_path / "scripts" / "experiment_ledger.json")
    monkeypatch.setattr(ratchet, "CONFIG_EXAMPLE", tmp_path / "config.example.json")
    monkeypatch.setattr(ratchet, "RUNBOOK", tmp_path / "runbook.md")
    return tmp_path


def _write_ledger(root: Path, toggles: dict, **overrides) -> None:
    ledger = {
        "schema": "experiment_ledger.v1",
        "review_date": "2999-01-01",
        "dynamic_call_sites": [
            {"file": "sms_tool/fake.py", "function": "dynamic", "reason": "test fixture"},
        ],
        "toggles": toggles,
    }
    ledger.update(overrides)
    path = root / "scripts" / "experiment_ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger), encoding="utf-8")


def _check(root: Path) -> list[str]:
    return ratchet.check(root)


# --------------------------------------------------------------------------
# extraction helpers
# --------------------------------------------------------------------------


def test_module_constants_resolve_a_constant_key():
    reads, dynamic = ratchet.code_reads(ROOT)
    # ``reference_lane.py`` passes LANE_KEY; it must resolve, not land in dynamic.
    assert "reference_lane" in reads
    assert all(path != "sms_tool/registration/reference_lane.py" for path, _fn, _line in dynamic)


def test_a_parameter_key_lands_in_dynamic():
    _reads, dynamic = ratchet.code_reads(ROOT)
    assert any(path.endswith("warmup.py") for path, _fn, _line in dynamic)


# --------------------------------------------------------------------------
# the real tree
# --------------------------------------------------------------------------


def test_the_real_ledger_satisfies_every_rule():
    assert ratchet.check() == []


def test_the_ledger_covers_every_toggle_the_code_reads():
    reads, _dynamic = ratchet.code_reads(ROOT)
    ledger = json.loads((ROOT / "scripts" / "experiment_ledger.json").read_text(encoding="utf-8"))
    assert set(reads) == set(ledger["toggles"])


# --------------------------------------------------------------------------
# sandbox: each rule fails on its own
# --------------------------------------------------------------------------


def test_the_sandbox_passes_before_it_is_broken(sandbox: Path):
    assert _check(sandbox) == []


def test_a_read_without_a_ledger_entry_fails(sandbox: Path):
    source = (sandbox / "sms_tool" / "fake.py").read_text(encoding="utf-8")
    (sandbox / "sms_tool" / "fake.py").write_text(
        source + '\n\ndef extra(config):\n    return registration_flag(config, "ghost_flag", False)\n',
        encoding="utf-8",
    )

    failures = _check(sandbox)

    assert any("ghost_flag: read at sms_tool/fake.py" in item for item in failures), failures


def test_a_ledger_entry_the_code_no_longer_reads_fails(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY), "stale_flag": dict(ENTRY, id="P9-8")})

    failures = _check(sandbox)

    assert any("stale_flag: no longer read by code" in item for item in failures), failures


def test_an_expired_open_entry_fails(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY, expires="2020-01-01")})

    failures = _check(sandbox)

    assert any("review date 2020-01-01 has passed" in item for item in failures), failures


def test_an_open_entry_without_any_expiry_fails(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY)})

    failures = _check(sandbox)

    assert failures == [], "the ledger-wide review_date is the fallback"
    _write_ledger(sandbox, {"known_flag": dict(ENTRY)}, review_date=None)

    failures = _check(sandbox)

    assert any("need expires or a ledger review_date" in item for item in failures), failures


def test_a_default_that_disagrees_with_the_example_config_fails(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY, default=True)})

    failures = _check(sandbox)

    assert any("default is True in the ledger but False in config.example.json" in item for item in failures), failures


def test_a_concluded_entry_that_is_still_read_needs_a_keep_reason(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY, status="concluded", verdict="adopted")})

    failures = _check(sandbox)

    assert any("explain why in keep.reason" in item for item in failures), failures


def test_a_retired_entry_that_is_still_read_fails(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY, status="retired")})

    failures = _check(sandbox)

    assert any("retired, but still read" in item for item in failures), failures


def test_an_invented_runbook_id_fails(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY, id="P7-77")})

    failures = _check(sandbox)

    assert any("runbook id 'P7-77' does not appear" in item for item in failures), failures


def test_a_key_the_runbook_never_names_fails(sandbox: Path):
    (sandbox / "runbook.md").write_text("| P9-9 | hypothesis | other | evidence |\n", encoding="utf-8")

    failures = _check(sandbox)

    assert any("never names registration.known_flag" in item for item in failures), failures


def test_an_undeclared_dynamic_call_site_fails(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY)}, dynamic_call_sites=[])

    failures = _check(sandbox)

    assert any("is not static -- declare it in dynamic_call_sites" in item for item in failures), failures


def test_a_feature_entry_may_not_claim_a_runbook_id(sandbox: Path):
    _write_ledger(sandbox, {"known_flag": dict(ENTRY, kind="feature")})

    failures = _check(sandbox)

    assert any("must not claim a runbook id" in item for item in failures), failures


def test_a_missing_ledger_is_reported_instead_of_passing(sandbox: Path):
    ratchet.LEDGER.unlink()

    assert _check(sandbox) == ["experiment_ledger.json: missing"]
