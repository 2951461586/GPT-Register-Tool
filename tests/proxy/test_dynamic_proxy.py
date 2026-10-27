"""Dynamic generation: rotate a session, screen it, enforce the ASN allowlist."""

from __future__ import annotations

import pytest

from sms_tool.proxy.dynamic import (
    DynamicProxyConfig,
    DynamicProxyFactory,
    ProxyGenerationError,
    generate_pool,
)
from sms_tool.proxy.screening import ScreeningSample

BASE = "http://user-region-VN-sid-0000-t-60:secret@vn.example:8080"


def _sample(proxy: str, *, asn: str = "AS9299", eligible: bool = True, reason: str = "") -> ScreeningSample:
    return ScreeningSample(
        index=0,
        proxy=proxy,
        eligible=eligible,
        asn=asn,
        country="VN",
        network_type="residential",
        quality_score=90,
        reason_code=reason,
    )


def _config(**overrides):
    return {
        "proxy": {
            "dynamic_generation": {
                "enabled": True,
                "base_proxy": BASE,
                "country": "VN",
                "asns": ["AS9299"],
                "count": 1,
                "max_attempts": 3,
                **overrides,
            }
        }
    }


def test_from_config_is_none_when_disabled_or_invalid():
    assert DynamicProxyConfig.from_config({"proxy": {"dynamic_generation": {"enabled": False}}}) is None
    assert DynamicProxyConfig.from_config(_config(country="VNN")) is None


def test_a_matching_eligible_session_is_returned_on_the_first_attempt():
    calls: list[str] = []

    def rotate(base, country):
        calls.append(country)
        return base + "#1"

    parsed = DynamicProxyConfig.from_config(_config())
    assert parsed is not None
    factory = DynamicProxyFactory(
        parsed,
        screen=lambda proxy, **kwargs: _sample(proxy),
        rotate=rotate,
    )
    assert factory.next_proxy().endswith("#1")
    assert calls == ["VN"]
    assert factory.last_screening["accepted"] is True
    assert factory.last_screening["asn"] == "AS9299"


def test_an_asn_mismatch_retries_until_the_allowlist_matches():
    seen: list[int] = []

    def rotate(base, country):
        seen.append(1)
        return f"{base}#{len(seen)}"

    def screen(proxy, **kwargs):
        return _sample(proxy, asn="AS1111" if len(seen) < 3 else "AS9299")

    parsed = DynamicProxyConfig.from_config(_config(max_attempts=3))
    assert parsed is not None
    factory = DynamicProxyFactory(
        parsed,
        screen=screen,
        rotate=rotate,
    )
    assert factory.next_proxy().endswith("#3")
    assert len(seen) == 3


def test_exhausted_attempts_raise_with_the_last_reason():
    parsed = DynamicProxyConfig.from_config(_config(max_attempts=2))
    assert parsed is not None
    factory = DynamicProxyFactory(
        parsed,
        screen=lambda proxy, **kwargs: _sample(proxy, asn="AS1111"),
        rotate=lambda base, country: base + "#x",
    )
    with pytest.raises(ProxyGenerationError, match="asn_mismatch"):
        factory.next_proxy()


def test_generate_pool_returns_empty_when_the_feature_is_off_or_generation_fails():
    assert generate_pool({}) == []
    assert (
        generate_pool(_config(), screen=lambda proxy, **kwargs: _sample(proxy, eligible=False, reason="datacenter"))
        == []
    )


def test_select_registration_proxy_pool_uses_generated_exits(monkeypatch):
    class FakeFactory:
        @classmethod
        def from_config(cls, config):
            return cls()

        def generate(self, count=None):
            return ["http://dynamic:1"]

    import sms_tool.proxy.dynamic as dynamic

    monkeypatch.setattr(dynamic, "DynamicProxyFactory", FakeFactory)
    from sms_tool.batch_runner import select_registration_proxy_pool

    selected = select_registration_proxy_pool(["http://static:1"])
    assert list(selected) == ["http://dynamic:1"]
