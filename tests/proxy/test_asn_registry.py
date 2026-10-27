"""ASN ledger: allocation rotation, hit rate, transport streak, opt-in wiring.

Ported from the reference project's ``tests/test_asn_performance.py`` cases that
are about policy. The allocation cursor is the part worth pinning: a
process-local cursor silently restarts the rotation after every restart.
"""

from __future__ import annotations

import pytest

from sms_tool.proxy import asn_registry
from sms_tool.proxy.asn_registry import (
    AsnRegistry,
    cached_asn_of,
    order_candidates,
    record_registration_outcome,
)

RESIDENTIAL_ROWS = [
    {"asn": "AS9299", "isp": "PLDT", "network_type": "residential", "country_code": "PH"},
    {"asn": "AS4837", "isp": "China Unicom", "network_type": "residential", "country_code": "CN"},
    {"asn": "AS14061", "isp": "DigitalOcean", "network_type": "datacenter", "country_code": "US"},
]


@pytest.fixture()
def registry(tmp_path) -> AsnRegistry:
    return AsnRegistry(tmp_path / "asn.sqlite3")


def test_configure_keeps_only_residential_and_mobile_candidates(registry):
    registry.configure(RESIDENTIAL_ROWS + [{"asn": "AS64500", "isp": "Telkomsel", "network_type": "mobile"}])
    asns = [row["asn"] for row in registry.rows()]
    assert asns == ["AS4837", "AS64500", "AS9299"]
    assert "AS14061" not in asns  # a datacenter ASN is a screening rejection


def test_rows_report_hit_rate_and_transport_rate(registry):
    registry.configure(RESIDENTIAL_ROWS)
    registry.record("AS9299", hit=True)
    registry.record("AS9299", hit=False)
    registry.record("AS9299", hit=False, valid=False)  # invalid sample: no denominator move
    registry.record_proxy_outcome("AS9299", ok=True)
    registry.record_proxy_outcome("AS9299", ok=False)
    row = registry.get("AS9299")
    assert row is not None
    assert row["valid_samples"] == 2
    assert row["hit_rate"] == 50.0
    assert row["transport_success_rate"] == 50.0
    assert row["consecutive_proxy_failures"] == 1


def test_allocate_round_robins_and_persists_the_cursor_across_instances(registry, tmp_path):
    registry.configure(RESIDENTIAL_ROWS)
    assert registry.allocate(1) == ["AS4837"]
    # A new instance opens the same file; the rotation must continue, not reset.
    reopened = AsnRegistry(tmp_path / "asn.sqlite3")
    assert reopened.allocate(1) == ["AS9299"]
    assert reopened.allocate(1) == ["AS4837"]  # wraps at two enabled ASNs


def test_allocate_ignores_disabled_asns(registry):
    registry.configure(RESIDENTIAL_ROWS)
    assert registry.set_enabled("AS4837", False, reason="手动停用") is True
    assert registry.allocate(3) == ["AS9299", "AS9299", "AS9299"]
    row = registry.get("AS4837")
    assert row is not None and row["enabled"] is False and row["disabled_reason"] == "手动停用"


def test_allocate_with_an_empty_ledger_returns_nothing(registry):
    assert registry.allocate(5) == []


def test_record_on_an_unknown_asn_is_a_no_op(registry):
    registry.record("AS99999", hit=True)
    assert registry.get("AS99999") is None


# ── wiring ───────────────────────────────────────────────────────────────────
def test_order_candidates_is_inert_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(asn_registry, "cached_asn_of", lambda proxy, **kwargs: "AS9299")
    pool = ["http://a:1", "http://b:2"]
    assert order_candidates(pool, config={}) == pool
    assert order_candidates(pool, config={"proxy": {"asn_allocation": {"enabled": False}}}) == pool


def test_order_candidates_puts_a_measured_asn_before_an_unmeasured_exit(monkeypatch, registry):
    ledger = registry
    ledger.configure(RESIDENTIAL_ROWS)
    measured = {"http://a:1": "AS9299", "http://b:2": "AS4837", "http://c:3": ""}
    monkeypatch.setattr(asn_registry, "cached_asn_of", lambda proxy, **kwargs: measured.get(proxy, ""))
    config = {"proxy": {"asn_allocation": {"enabled": True}}}

    ordered = order_candidates(
        ["http://c:3", "http://a:1", "http://b:2"],
        config=config,
        registry=ledger,
    )
    # First allocation slot is AS4837, so its proxy leads; the unmeasured tail
    # stays reachable but last.
    assert ordered == ["http://b:2", "http://a:1", "http://c:3"]


def test_order_candidates_fails_open_when_the_ledger_cannot_open(monkeypatch, tmp_path):
    class Broken(AsnRegistry):
        def __init__(self, *_args, **_kwargs):  # noqa: D401 - test double
            raise RuntimeError("disk full")

    monkeypatch.setattr(asn_registry, "AsnRegistry", Broken)
    pool = ["http://a:1", "http://b:2"]
    assert order_candidates(pool, config={"proxy": {"asn_allocation": {"enabled": True}}}) == pool


def test_record_registration_outcome_ignores_an_unmeasured_proxy(monkeypatch):
    monkeypatch.setattr(asn_registry, "cached_asn_of", lambda proxy, **kwargs: "")
    assert record_registration_outcome("http://a:1", ok=True, config={}) == ""


def test_record_registration_outcome_attributed_to_the_measured_asn(tmp_path, registry, monkeypatch):
    registry.configure(RESIDENTIAL_ROWS)
    monkeypatch.setattr(asn_registry, "cached_asn_of", lambda proxy, **kwargs: "AS9299")
    # ``registry`` passed explicitly so the test never touches ``runtime/``.
    assert record_registration_outcome("http://a:1", ok=True, config={}, registry=registry) == "AS9299"
    row = registry.get("AS9299")
    assert row is not None
    assert row["valid_samples"] == 1
    assert row["hit_rate"] == 100.0


def test_cached_asn_of_reads_the_shared_geo_cache_without_probing(monkeypatch):
    from sms_tool.geo.resolver import ProxyGeo, reset_shared_geo_resolver, shared_geo_resolver

    reset_shared_geo_resolver()
    shared_geo_resolver().remember("http://measured:1", ProxyGeo(country="PH", asn="AS9299"))
    try:
        assert cached_asn_of("http://measured:1") == "AS9299"
    finally:
        reset_shared_geo_resolver()
    assert cached_asn_of("http://never-probed:2") == ""


def test_configure_persists_the_proxy_to_asn_mapping(registry):
    proxy = "http://user-region-PH-sid-abc-t-60:secret@ph.example:8080"
    registry.configure([{**RESIDENTIAL_ROWS[0], "proxy": proxy}])
    # Same endpoint + same sticky session resolves; a different session does not.
    assert registry.asn_of(proxy) == "AS9299"
    other = "http://user-region-PH-sid-other-t-60:secret@ph.example:8080"
    assert registry.asn_of(other) == ""


def test_cached_asn_of_falls_back_to_the_persisted_mapping(monkeypatch):
    from sms_tool.geo.resolver import reset_shared_geo_resolver

    reset_shared_geo_resolver()
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        ledger = AsnRegistry(Path(directory) / "asn.sqlite3")
        proxy = "http://user-region-PH-sid-abc-t-60:secret@ph.example:8080"
        ledger.configure([{**RESIDENTIAL_ROWS[0], "proxy": proxy}])
        try:
            assert cached_asn_of(proxy, registry=ledger) == "AS9299"
        finally:
            reset_shared_geo_resolver()


def test_the_proxy_url_never_reaches_metadata_json(registry):
    proxy = "http://user-region-PH-sid-abc-t-60:secret@ph.example:8080"
    registry.configure([{**RESIDENTIAL_ROWS[0], "proxy": proxy}])
    row = registry.get("AS9299")
    assert row is not None
    assert "secret" not in str(row)
    # The persisted mapping still exists, but only as a credential-free key.
    assert registry.asn_of(proxy) == "AS9299"
