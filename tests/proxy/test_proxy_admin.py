"""Proxy-pool admin: list masking, dual-signal test, screening payload.

Every network call is injected. The properties pinned here are the ones a UI
depends on: credentials never appear, the edge verdict and the geo measurement
stay two separate answers, and an empty pool is not an error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sms_tool.geo.resolver import ProxyGeo, reset_shared_geo_resolver, shared_geo_resolver
from sms_tool.proxy import admin

CONFIG = {
    "proxy": {
        "lanes": {
            "protocol_registration": {"pool": ["http://user:secret@ph1.example:8080", "socks5://ph2.example:1080"]}
        }
    }
}


@pytest.fixture(autouse=True)
def _fresh_geo_cache():
    reset_shared_geo_resolver()
    yield
    reset_shared_geo_resolver()


def test_list_pool_masks_credentials_and_marks_auth():
    payload = admin.list_pool(CONFIG)
    assert payload["ok"] is True
    assert payload["count"] == 2
    first, second = payload["proxies"]
    assert "secret" not in first["proxy"]
    assert first["proxy"] == "http://***:***@ph1.example:8080"
    assert first["has_auth"] is True
    assert "proxy_raw" not in first  # masked entries are never copyable
    assert second["proxy"] == "socks5://ph2.example:1080"
    assert second["proxy_raw"] == "socks5://ph2.example:1080"


def test_list_pool_uses_the_cached_geo_without_probing(monkeypatch):
    shared_geo_resolver().remember(
        "http://user:secret@ph1.example:8080",
        ProxyGeo(country="PH", asn="AS9299", isp="PLDT", network_type="residential"),
    )
    payload = admin.list_pool(CONFIG)
    first = payload["proxies"][0]
    assert first["measured"] is True
    assert first["country"] == "PH"
    assert first["asn"] == "AS9299"
    assert payload["measured"] == 1
    assert payload["asns"] == [
        {
            "asn": "AS9299",
            "isp": "PLDT",
            "count": 1,
            "network_type": "residential",
            "countries": ["PH"],
        }
    ]


def test_list_pool_with_an_empty_lane_is_not_an_error():
    payload = admin.list_pool({"proxy": {"lanes": {}}}, lane="missing")
    assert payload == {
        "ok": True,
        "lane": "missing",
        "count": 0,
        "measured": 0,
        "proxies": [],
        "asns": [],
        # The list carries the config-wide lane-isolation verdict so the desktop
        # window can show it without a second command; an empty lane must not
        # change that (see proxy/registry.lane_isolation).
        "lane_isolation": {
            "ok": True,
            "overlaps": {"mailbox": [], "payment_qualification": []},
            "conflicts": {},
            "acknowledged": [],
        },
    }


def test_list_pool_reports_a_lane_overlap_without_failing():
    """A shared exit is a warning on the listing, never an error that empties it."""
    cred = "http://u:p@shared.example:1"
    config = {"proxy": {"pool": [cred], "lanes": {"payment_qualification": [cred]}}}
    payload = admin.list_pool(config)

    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["lane_isolation"]["ok"] is False
    assert payload["lane_isolation"]["conflicts"] == {"payment_qualification": ["shared.example:1"]}


def test_resolve_pool_explicit_input_wins_over_the_lane():
    assert admin.resolve_pool(CONFIG, explicit="http://explicit:1") == ["http://explicit:1"]
    assert admin.resolve_pool(CONFIG) == [
        "http://user:secret@ph1.example:8080",
        "socks5://ph2.example:1080",
    ]


def test_test_pool_reports_edge_and_geo_as_two_signals():
    def edge_probe(proxy, *, timeout):
        status = "clean" if "good" in proxy else "blocked"
        return SimpleNamespace(status=status, http_status=200, error="")

    def geo_probe(proxy, **kwargs):
        if "good" in proxy:
            return ProxyGeo(ip="1.1.1.1", country="PH", asn="AS9299", org="PLDT", network_type="residential")
        return ProxyGeo(ip="2.2.2.2", country="US", asn="AS13335", org="Cloudflare", network_type="datacenter")

    payload = admin.test_pool(
        ["http://good:1", "http://bad:2"],
        country="PH",
        workers=2,
        edge_probe=edge_probe,
        geo_probe=geo_probe,
    )
    assert payload["total"] == 2
    assert payload["reachable"] == 2  # blocked is still reachable; dead is not
    good, bad = payload["results"]
    assert good["country"] == "PH" and good["country_match"] is True
    assert good["residential"] is True
    assert bad["country_match"] is False and bad["residential"] is False
    assert "secret" not in str(payload)


def test_test_pool_treats_a_raising_edge_probe_as_dead():
    def edge_probe(proxy, *, timeout):
        raise RuntimeError("connect refused")

    payload = admin.test_pool(["http://x:1"], edge_probe=edge_probe, geo_probe=lambda *a, **k: ProxyGeo())
    row = payload["results"][0]
    assert row["edge"] == "dead"
    assert row["reachable"] is False
    assert "connect refused" in row["edge_error"]


def test_test_pool_with_no_entries_is_an_empty_success():
    assert admin.test_pool([]) == {"ok": True, "total": 0, "results": []}


def test_screen_report_returns_counts_and_the_asn_ranking_without_credentials():
    def probe(proxy, *, timeout, endpoints, need_timezone):
        if "good" in proxy:
            return ProxyGeo(ip="1.1.1.1", country="PH", asn="AS9299", org="PLDT", network_type="residential")
        return ProxyGeo()

    payload = admin.screen_report(
        ["http://user:secret@good:1", "http://bad:2"],
        country="PH",
        workers=1,
        probe=probe,
    )
    assert payload["counts"]["total"] == 2
    assert payload["counts"]["eligible"] == 1
    assert [row["asn"] for row in payload["asns"]] == ["AS9299"]
    assert payload["datacenter"] == 0
    # Credentials are masked; an auth-free entry keeps its (identical) masked form.
    assert "secret" not in str(payload)
    assert "http://***:***@good:1" in str(payload)
    assert "http://bad:2" in str(payload)


def test_list_pool_does_not_call_a_hint_only_cache_entry_measured(monkeypatch):
    # ``remember`` is documented as an externally *measured* answer and stamps
    # source="probe"; the hint-only shape comes from the resolver's own hint
    # short-circuit (no network, country only).
    shared_geo_resolver().resolve("http://user:secret@ph1.example:8080", hint="PH", need_timezone=False)
    payload = admin.list_pool(CONFIG)
    first = payload["proxies"][0]
    assert first["country"] == "PH"
    assert first["measured"] is False
    assert first["source"] == "hint"


def test_screen_report_records_residential_samples_in_the_asn_ledger(monkeypatch):
    import sms_tool.proxy.asn_registry as asn_registry

    recorded: list[dict] = []

    class FakeLedger:
        def configure(self, rows):
            recorded.extend(rows)
            return []

    monkeypatch.setattr(asn_registry.AsnRegistry, "from_config", classmethod(lambda cls, config: FakeLedger()))

    def probe(proxy, *, timeout, endpoints, need_timezone):
        return ProxyGeo(ip="1.1.1.1", country="PH", asn="AS9299", org="PLDT", network_type="residential")

    admin.screen_report(["http://user:secret@good:1"], country="PH", workers=1, probe=probe, config={"proxy": {}})
    assert recorded
    assert recorded[0]["proxy"] == "http://user:secret@good:1"
    assert recorded[0]["asn"] == "AS9299"
