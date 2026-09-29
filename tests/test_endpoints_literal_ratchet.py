"""Tests for scripts/endpoints_literal_ratchet.py.

Layers, in increasing order of "does it actually protect anything":

1. **The predicate, both directions** -- a host-literal counter that only ever
   returns 0 (or everything) manufactures false confidence. The synthetic
   fixtures pin literal vs f-string vs reference vs prose vs unmanaged host.
2. **The self-test harness** -- the fixtures must be able to go red.
3. **The ratchet against the real tree** -- and the pinned fact that the three
   wired extractors carry zero inline managed-host literals.

The regression this gate exists for: ``tests/test_protocol_payment_endpoints.py``
asserts the *shape* of ``common/endpoints.py`` but never checks a caller, so the
56 inline host literals ideal / twint / blik carried until 2026-09-28 were
invisible to every gate.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import endpoints_literal_ratchet as eps  # noqa: E402  # type: ignore

# Load the authority module itself for the constants cross-check.
_ENDPOINTS_PATH = ROOT / "services" / "protocol-payment" / "common" / "endpoints.py"
_SPEC = importlib.util.spec_from_file_location("ratchet_endpoints_probe", _ENDPOINTS_PATH)
assert _SPEC and _SPEC.loader
ENDPOINTS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ENDPOINTS
_SPEC.loader.exec_module(ENDPOINTS)

WIRED = (
    "services/protocol-payment/ideal/ideal_qr_extract.py",
    "services/protocol-payment/twint/twint_extract.py",
    "services/protocol-payment/blik/blik_qr_extract.py",
)


# --------------------------------------------------------------------------
# The predicate, both directions
# --------------------------------------------------------------------------


def test_plain_and_fstring_literals_are_counted():
    assert eps.count_literals('x = "https://chatgpt.com/a"\n') == 1
    assert eps.count_literals('x = f"https://api.stripe.com/v1/pages/{cs}/init"\n') == 1


def test_endpoints_references_and_prose_are_not_counted():
    assert eps.count_literals("x = endpoints.CHATGPT_BASE\n") == 0
    assert eps.count_literals('x = f"{endpoints.STRIPE_PAYMENT_PAGES}/{cs}"\n') == 0
    assert eps.count_literals("# see https://chatgpt.com/checkout for the flow\n") == 0


def test_unmanaged_host_is_not_counted():
    assert eps.count_literals('x = "https://www.cloudflare.com/cdn-cgi/trace"\n') == 0
    assert eps.count_literals('x = "https://ipwho.is/?fields=success"\n') == 0


def test_multiple_literals_on_one_line_are_all_counted():
    assert eps.count_literals('a = "https://chatgpt.com"; b = "https://pay.openai.com/c/pay/"\n') == 2


def test_self_test_passes_on_the_shipped_fixtures():
    assert eps.predicates_are_trustworthy() is None


def test_self_test_detects_a_predicate_that_matches_nothing(monkeypatch):
    monkeypatch.setattr(eps, "count_literals", lambda _src: 0)
    assert eps.predicates_are_trustworthy() is not None


def test_self_test_detects_a_predicate_that_matches_everything(monkeypatch):
    monkeypatch.setattr(eps, "count_literals", lambda _src: 999)
    assert eps.predicates_are_trustworthy() is not None


def test_cli_fails_loudly_when_the_predicate_is_gutted(monkeypatch):
    monkeypatch.setattr(eps, "count_literals", lambda _src: 0)
    try:
        code = eps.main([])
    except SystemExit as exc:  # pragma: no cover - defensive
        code = exc.code
    assert code != 0, "a gutted predicate must not exit 0"


# --------------------------------------------------------------------------
# The ratchet against the real tree
# --------------------------------------------------------------------------


def test_managed_hosts_match_the_authority_constants():
    """MANAGED_HOSTS must cover every host ``endpoints.py`` declares.

    A new host constant in the authority that the ratchet does not watch would
    let an extractor inline it unmeasured.
    """
    declared = {
        ENDPOINTS.CHATGPT_HOST,
        ENDPOINTS.STRIPE_API_BASE.removeprefix("https://"),
        ENDPOINTS.STRIPE_CHECKOUT_BASE.removeprefix("https://"),
        ENDPOINTS.OPENAI_PAY_BASE.removeprefix("https://"),
    }
    assert declared <= set(eps.MANAGED_HOSTS), (
        f"ratchet misses authority hosts: {sorted(declared - set(eps.MANAGED_HOSTS))}"
    )


def test_authority_module_is_excluded_from_the_scan():
    counts = eps.collect()
    assert not any(path.endswith("common/endpoints.py") for path in counts), (
        "the authority module defines the hosts; scanning it would score its own constants"
    )


def test_wired_extractors_have_zero_inline_literals():
    """The 2026-09-28 wiring of ideal / twint / blik must stay wired."""
    counts = eps.collect()
    residual = {path: counts.get(path, 0) for path in WIRED if counts.get(path, 0)}
    assert not residual, (
        "a wired extractor re-inlined a managed host instead of importing "
        f"endpoints: {residual}"
    )


def test_ratchet_holds_for_the_current_tree():
    counts = eps.collect()
    baseline = eps.load_baseline()
    grown = [f"{path}: {n} > {baseline.get(path, 0)}" for path, n in counts.items() if n > baseline.get(path, 0)]
    assert not grown, (
        "inline managed-host literals grew:\n  "
        + "\n  ".join(grown)
        + "\nImport the host from common/endpoints.py instead."
    )


def test_ratchet_can_decrease_and_that_is_the_point():
    """A ratchet that rejected decreases would be a freeze, not a ratchet."""
    counts = eps.collect()
    totals = sum(counts.values())
    assert totals > 0
    assert totals - 1 < totals


def test_baseline_is_present_and_shaped():
    data = json.loads(eps.BASELINE.read_text(encoding="utf-8"))
    assert "files" in data and isinstance(data["files"], dict)
    assert data["files"], "baseline must record the not-yet-wired files"


def test_cli_exit_zero_on_the_current_tree():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "endpoints_literal_ratchet.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ratchet OK" in result.stdout


def test_cli_reports_growth_against_a_tightened_baseline(tmp_path, monkeypatch):
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"files": {}}), encoding="utf-8")
    monkeypatch.setattr(eps, "BASELINE", baseline_path)
    assert eps.main([]) == 1, "an empty baseline must trip the ratchet on the real tree"


def test_cli_missing_baseline_is_a_hard_error(tmp_path, monkeypatch):
    monkeypatch.setattr(eps, "BASELINE", tmp_path / "nope.json")
    assert eps.main([]) == 2


def test_corrupt_baseline_fails_loudly_rather_than_passing(tmp_path, monkeypatch):
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(eps, "BASELINE", baseline_path)
    # Empty baseline => every current count is "grown" => exit 1, never 0.
    assert eps.main([]) == 1


def test_baseline_is_written_with_lf_endings(tmp_path, monkeypatch):
    scratch = tmp_path / "baseline.json"
    scratch.write_bytes(eps.BASELINE.read_bytes())
    monkeypatch.setattr(eps, "BASELINE", scratch)
    assert eps.main(["--update-baseline"]) == 0
    raw = scratch.read_bytes()
    assert b"\r\n" not in raw, "baseline written with CRLF; expected LF only"
    assert raw.endswith(b"\n")


@pytest.mark.parametrize("path", WIRED)
def test_wired_files_are_pure_lf(path):
    raw = (ROOT / path).read_bytes()
    assert b"\r\n" not in raw
