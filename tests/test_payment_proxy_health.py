"""Tests for the shared proxy health/geo cache and PayPal country catalog."""

import pytest

from sms_tool import paypal_proxy
from sms_tool.paypal_proxy import (
    PayPalProxyState,
    ProxyProbeResult,
    probe_proxy,
    select_proxy_from_pool,
)
from sms_tool.payment_country_catalog import is_paypal_supported, validate_paypal_country


def test_probe_cache_serves_second_selection_without_network(tmp_path, monkeypatch):
    state = PayPalProxyState(tmp_path / "state.json", probe_cache_ttl_seconds=600)
    calls = []

    def fake_net(value, expected, stage, timeout):
        calls.append(value)
        return ProxyProbeResult(
            ok=True, stage=stage, expected_country=expected,
            ip="1.2.3.4", country_code=expected or "US", country="X",
        )

    monkeypatch.setattr(paypal_proxy, "_probe_proxy_network", fake_net)
    proxy = "http://user-region-US:pass@host:1000"
    first = probe_proxy(proxy, "US", "checkout", state=state)
    second = probe_proxy(proxy, "US", "checkout", state=state)

    assert first.ok and second.ok
    assert first.country_code == "US"
    # Second probe of the same proxy key is served from the cache, not the net.
    assert len(calls) == 1


def test_no_state_probe_is_never_cached(tmp_path, monkeypatch):
    calls = []

    def fake_net(value, expected, stage, timeout):
        calls.append(value)
        return ProxyProbeResult(ok=True, stage=stage, expected_country=expected, ip="1", country_code="US", country="US")

    monkeypatch.setattr(paypal_proxy, "_probe_proxy_network", fake_net)
    proxy = "http://user-region-US:pass@host:1000"
    probe_proxy(proxy, "US", "checkout")
    probe_proxy(proxy, "US", "checkout")
    assert len(calls) == 2  # without state, every probe hits the network


def test_select_pool_skips_cooldown_and_prefers_healthy(tmp_path, monkeypatch):
    state = PayPalProxyState(
        tmp_path / "state.json",
        fail_skip_after=1,
        fail_cooldown_seconds=600,
        probe_cache_ttl_seconds=0,
    )
    bad = "http://user-region-US-sid-aaaa-t-5:pass@bad-exit:1000"
    good = "http://user-region-US-sid-bbbb-t-5:pass@good-exit:1000"
    # Push the first pool entry into its failure cooldown.
    state.record_result("checkout", bad, False, "timeout", "US")

    probed: list[str] = []

    def fake_net(value, expected, stage, timeout):
        probed.append(value)
        return ProxyProbeResult(ok=True, stage=stage, expected_country=expected, ip="1", country_code="US", country="US")

    monkeypatch.setattr(paypal_proxy, "_probe_proxy_network", fake_net)
    selected, attempts = select_proxy_from_pool([bad, good], "US", "checkout", state=state)

    assert "good-exit" in selected
    # The cooling-down bad proxy is ranked out and never probed.
    assert all("bad-exit" not in value for value in probed)


def test_paypal_supported_country_catalog():
    assert is_paypal_supported("US")
    assert is_paypal_supported("gb")  # case-insensitive
    assert is_paypal_supported("VN")
    assert not is_paypal_supported("TR")  # PayPal withdrew from Turkey
    assert not is_paypal_supported("")


def test_validate_paypal_country_is_compatibility_noop():
    validate_paypal_country("gopay", "TR")
    validate_paypal_country("paypal", "")
    validate_paypal_country("paypal", "US")
    validate_paypal_country("paypal", "TR")
