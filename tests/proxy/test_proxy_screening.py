"""Proxy screening: IP-intelligence classification, dual-pass verdict, ASN ranking.

Ports the reference project's ``tests/test_proxy_screening.py`` cases that are
about the *policy* (not the 711proxy paste formats, which this repository owns
in ``proxy/entry.py``). Every network call is faked: the point is that a
screening verdict is a pure function of two measured passes.
"""

from __future__ import annotations

import pytest

from sms_tool.geo.resolver import ProxyGeo, normalize_geo_response
from sms_tool.proxy import screening
from sms_tool.proxy.screening import (
    NETWORK_DATACENTER,
    NETWORK_MOBILE,
    NETWORK_RESIDENTIAL,
    NETWORK_UNKNOWN,
    REASON_ASN_CHANGED,
    REASON_COUNTRY_MISMATCH,
    REASON_DATACENTER,
    REASON_INTEL_INSUFFICIENT,
    REASON_INTEL_UNKNOWN,
    REASON_OK,
    REASON_PROXY_DETECTED,
    REASON_PROXY_FAILED,
    REASON_UNSTABLE_EXIT,
    ScreeningPass,
    aggregate_by_asn,
    classify_network_type,
    evaluate_sample,
    mask_proxy,
    sanitize_proxy_text,
    score_probe_quality,
    screen_pool,
    screen_proxy,
    summarize,
)

RESIDENTIAL = ProxyGeo(
    ip="49.144.165.95",
    country="PH",
    timezone="Asia/Manila",
    org="AS9299 Philippine Long Distance Telephone Co.",
    asn="AS9299",
    isp="Philippine Long Distance Telephone Co.",
)
DATACENTER = ProxyGeo(
    ip="203.0.113.9",
    country="PH",
    timezone="Asia/Manila",
    org="AS13335 Cloudflare, Inc.",
    asn="AS13335",
)


def _pass(**overrides) -> ScreeningPass:
    base = ScreeningPass.from_geo(RESIDENTIAL, latency_ms=80)
    return base if not overrides else ScreeningPass(**{**base.__dict__, **overrides})


# ── intel normalization ───────────────────────────────────────────────────────
def test_normalize_extracts_asn_isp_and_reverse_dns():
    geo = normalize_geo_response(
        {
            "ip": "1.2.3.4",
            "country_code": "JP",
            "org": "AS2516 KDDI CORPORATION",
            "isp": "KDDI",
            "hostname": "p1234-ab.example.jp",
            "timezone": {"id": "Asia/Tokyo"},
        }
    )
    assert geo.asn == "AS2516"
    assert geo.isp == "KDDI"
    assert geo.reverse_dns == "p1234-ab.example.jp"


def test_normalize_reads_ipwho_connection_block_and_bare_numeric_asn():
    geo = normalize_geo_response(
        {
            "ip": "49.144.165.95",
            "country_code": "PH",
            "connection": {"asn": 9299, "org": "PLDT", "isp": "PLDT"},
            "timezone": {"id": "Asia/Manila"},
        }
    )
    assert geo.asn == "AS9299"
    assert geo.isp == "PLDT"


def test_normalize_reads_privacy_flags_from_security_or_toplevel():
    nested = normalize_geo_response({"security": {"hosting": True, "vpn": False}})
    assert nested.is_hosting is True
    top_level = normalize_geo_response({"proxy": True})
    assert top_level.is_proxy is True


def test_to_dict_carries_the_intel_fields():
    payload = RESIDENTIAL.to_dict()
    assert payload["asn"] == "AS9299"
    assert payload["network_type"] == "unknown"  # classification is screening's job


def test_resolver_merge_keeps_intel_when_a_thinner_probe_answers(monkeypatch):
    """A country-only re-probe must not erase the ASN the first answer measured."""
    from sms_tool.geo.resolver import GeoResolver

    answers = iter([RESIDENTIAL, ProxyGeo(ip="49.144.165.95", country="PH", source="probe")])
    resolver = GeoResolver(probe=lambda *a, **k: next(answers))
    first = resolver.resolve("http://p:1", need_timezone=True)
    second = resolver.resolve("http://p:1", need_timezone=True, verify_hint=True)
    assert first.asn == "AS9299"
    assert second.asn == "AS9299"


# ── classification ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("intel", "expected"),
    [
        ({"country": "PH", "org": "PLDT"}, NETWORK_RESIDENTIAL),
        ({"country": "PH", "org": "DigitalOcean LLC"}, NETWORK_DATACENTER),
        ({"country": "ID", "org": "Telkomsel cellular"}, NETWORK_MOBILE),
        ({"country": "PH"}, NETWORK_UNKNOWN),
        (None, NETWORK_UNKNOWN),
    ],
)
def test_classify_network_type(intel, expected):
    assert classify_network_type(intel) == expected


def test_explicit_hosting_flag_beats_a_residential_looking_org():
    # A resold residential block routed through a datacenter is still a
    # datacenter exit; the provider's own flag is the stronger evidence.
    assert classify_network_type({"country": "PH", "org": "PLDT", "privacy": {"hosting": True}}) == NETWORK_DATACENTER


def test_pass_confidence_reflects_evidence_not_certainty():
    one_source = ScreeningPass.from_geo(RESIDENTIAL)
    two_sources = ScreeningPass.from_geo(
        ProxyGeo(**{**RESIDENTIAL.to_dict(), "intel_sources": 2})  # type: ignore[arg-type]
    )
    assert 0 < one_source.residential_confidence < two_sources.residential_confidence <= 100
    assert ScreeningPass.from_geo(DATACENTER).residential_confidence == 0


# ── verdict ──────────────────────────────────────────────────────────────────
def test_accepts_a_stable_residential_exit_in_the_target_country():
    eligible, code, _ = evaluate_sample(_pass(), _pass(), "PH")
    assert eligible is True
    assert code == REASON_OK


def test_rejects_an_unstable_exit():
    eligible, code, _ = evaluate_sample(_pass(), _pass(ip="49.144.165.96"), "PH")
    assert (eligible, code) == (False, REASON_UNSTABLE_EXIT)


def test_rejects_a_country_mismatch():
    eligible, code, _ = evaluate_sample(_pass(), _pass(country="ID"), "PH")
    assert (eligible, code) == (False, REASON_COUNTRY_MISMATCH)


def test_rejects_an_asn_change_between_passes():
    eligible, code, _ = evaluate_sample(_pass(), _pass(asn="AS4837"), "PH")
    assert (eligible, code) == (False, REASON_ASN_CHANGED)


def test_rejects_proxy_flagged_evidence_even_when_the_org_looks_residential():
    flagged = _pass(is_vpn=True, residential_confidence=0)
    eligible, code, _ = evaluate_sample(_pass(), flagged, "PH")
    assert (eligible, code) == (False, REASON_PROXY_DETECTED)


def test_rejects_a_datacenter_exit():
    # Same exit IP as the residential pass so only the network classification
    # differs -- otherwise the stability check would report first.
    datacenter = ScreeningPass.from_geo(
        ProxyGeo(
            ip=RESIDENTIAL.ip,
            country="PH",
            org="DigitalOcean, LLC",
            asn="AS9299",
            timezone="Asia/Manila",
        )
    )
    eligible, code, _ = evaluate_sample(_pass(), datacenter, "PH")
    assert (eligible, code) == (False, REASON_DATACENTER)


def test_rejects_thin_evidence():
    thin = _pass(network_type=NETWORK_UNKNOWN, residential_confidence=0)
    assert evaluate_sample(_pass(), thin, "PH")[:2] == (False, REASON_INTEL_UNKNOWN)


def test_rejects_a_probe_failure_with_its_own_error_code():
    failed = ScreeningPass(ok=False, error_code=REASON_PROXY_FAILED, error="timeout")
    assert evaluate_sample(_pass(), failed, "PH")[:2] == (False, REASON_PROXY_FAILED)


def test_quality_floor_rejects_thin_but_technically_residential_evidence():
    eligible, code, reason = evaluate_sample(_pass(), _pass(intel_sources=1), "PH", quality_min=95)
    assert (eligible, code) == (False, REASON_INTEL_INSUFFICIENT)
    assert "质量分" in reason


# ── quality score ────────────────────────────────────────────────────────────
def test_quality_score_rewards_agreement_and_two_sources():
    strong = score_probe_quality(_pass(intel_sources=2), _pass(intel_sources=2))
    weak = score_probe_quality(_pass(intel_sources=1), _pass(intel_sources=1))
    assert strong > weak
    assert 0 <= weak <= strong <= 100


def test_quality_score_penalises_privacy_flags_below_any_threshold():
    score = score_probe_quality(_pass(is_proxy=True), _pass(is_proxy=True))
    assert score == 0


def test_quality_score_of_a_failed_pass_is_zero():
    assert score_probe_quality(ScreeningPass(), _pass()) == 0


# ── dual pass wiring ─────────────────────────────────────────────────────────
def test_screen_proxy_uses_two_uncached_passes_in_opposite_order():
    orders: list[tuple] = []

    def fake_probe(proxy, *, timeout, endpoints, need_timezone):
        orders.append(tuple(endpoints))
        return RESIDENTIAL

    sample = screen_proxy("http://user:pw@ph.example:8080", country="PH", probe=fake_probe)
    assert len(orders) == 2
    assert orders[0] != orders[1]
    assert tuple(reversed(orders[0])) == orders[1]
    assert sample.eligible is True
    assert sample.second.intel_sources == 2
    # credentials never leak into the display shape
    assert "user" not in sample.masked_proxy and "pw" not in sample.masked_proxy


def test_screen_proxy_reports_a_raising_probe_as_a_failed_pass():
    def broken(proxy, **kwargs):
        raise RuntimeError("tls reset")

    sample = screen_proxy("http://p:1", country="PH", probe=broken)
    assert sample.eligible is False
    assert sample.reason_code == REASON_PROXY_FAILED
    assert "tls reset" in sample.first.error


def test_screen_pool_preserves_input_order_and_counts():
    def fake_probe(proxy, *, timeout, endpoints, need_timezone):
        return RESIDENTIAL if "good" in proxy else ProxyGeo()

    samples = screen_pool(
        ["http://good1:1", "http://bad:2", "http://good2:3"],
        country="PH",
        workers=2,
        probe=fake_probe,
    )
    assert [item.masked_proxy for item in samples] == [
        "http://good1:1",
        "http://bad:2",
        "http://good2:3",
    ]
    assert [item.eligible for item in samples] == [True, False, True]
    summary = summarize(samples)
    assert summary == {"total": 3, "eligible": 2, "rejected": 1, "reasons": {REASON_PROXY_FAILED: 1}, "asns": 1}


# ── ASN aggregation ──────────────────────────────────────────────────────────
def test_aggregate_by_asn_ranks_the_eligible_group_first_and_keeps_rates():
    good = screen_proxy(
        "http://good:1",
        country="PH",
        probe=lambda *a, **k: RESIDENTIAL,
    )
    bad = screen_proxy(
        "http://bad:2",
        country="PH",
        probe=lambda *a, **k: ProxyGeo(),
    )
    rows = aggregate_by_asn([bad, good])
    assert [row["asn"] for row in rows] == ["AS9299"]
    row = rows[0]
    assert row["sample_count"] == 1
    assert row["eligible_count"] == 1
    assert row["stability_rate"] == 100.0
    assert row["residential_rate"] == 100.0
    assert row["median_quality_score"] > 0


def test_aggregate_by_asn_ignores_samples_without_an_asn():
    empty = screen_proxy("http://x:1", country="PH", probe=lambda *a, **k: ProxyGeo())
    assert aggregate_by_asn([empty]) == []


# ── redaction ────────────────────────────────────────────────────────────────
def test_sanitize_proxy_text_masks_url_and_bare_credentials():
    assert sanitize_proxy_text("connect to http://user:secret@host:8080 failed") == (
        "connect to http://***:***@host:8080 failed"
    )
    assert sanitize_proxy_text("user:secret:host:8080") == "***:***:host:8080"
    # The provider UI's other paste order: host:port:user:pass.
    assert sanitize_proxy_text("host:8080:user:secret") == "host:8080:***:***"


def test_mask_proxy_keeps_scheme_host_port_only():
    assert mask_proxy("socks5h://user:pw@geo.example:1080") == "socks5h://***:***@geo.example:1080"
    assert mask_proxy("http://geo.example:8080") == "http://geo.example:8080"
    assert mask_proxy("host:8080:user:secret") == "host:8080:***:***"
    assert mask_proxy("user:secret:host:8080") == "***:***:host:8080"


def test_report_masks_every_credential():
    sample = screen_proxy("http://user:pw@ph.example:8080", country="PH", probe=lambda *a, **k: RESIDENTIAL)
    report = screening.format_report([sample])
    assert "user" not in report and "pw@" not in report
    assert "AS9299" in report
