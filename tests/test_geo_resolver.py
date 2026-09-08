"""Tests for ``sms_tool.geo`` — the single-authority exit-geo resolver (P0-1).

Before this package existed exit-country detection was implemented three times
(``proxy_entry.infer_region`` / ``paypal_proxy._probe_proxy_network`` /
``browser_fingerprint_pool._query_geo_endpoints``) with three endpoint lists and
two caches that never shared an answer.  The tests below pin the behaviour that
makes the shared resolver safe to route everything through:

- the hint is free and short-circuits the probe
- a raising probe degrades to "unknown" instead of breaking registration
- a failure expires (it does not poison the process forever)
- a measurement published by one caller is reused by every other caller
"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from sms_tool import geo
from sms_tool.geo.resolver import (
    GEO_ENDPOINTS,
    NEGATIVE_TTL,
    POSITIVE_TTL,
    GeoResolver,
    ProxyGeo,
    normalize_geo_response,
    probe_exit_geo,
    reset_shared_geo_resolver,
)
from sms_tool.browser_fingerprint_pool import detect_proxy_exit_geo


class _RaisingProbe:
    """Stand-in for a probe that blows up (network down, patched test seam…)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, proxy, *, timeout, endpoints, need_timezone):
        self.calls += 1
        raise RuntimeError("network down")


class _FakeProbe:
    def __init__(self, result: ProxyGeo):
        self.result = result
        self.calls = 0
        self.seen: list[str] = []

    def __call__(self, proxy, *, timeout, endpoints, need_timezone):
        self.calls += 1
        self.seen.append(proxy)
        return self.result


def _resolver(**kwargs) -> GeoResolver:
    """A resolver with caching on but TTLs long enough to be deterministic."""
    kwargs.setdefault("positive_ttl", 10_000.0)
    kwargs.setdefault("negative_ttl", 10_000.0)
    return GeoResolver(**kwargs)


class NormalizeTests(unittest.TestCase):
    def test_ipinfo_shape(self):
        geo_out = normalize_geo_response(
            {"ip": "1.2.3.4", "country": "JP", "region": "Tokyo",
             "city": "Shibuya", "timezone": "Asia/Tokyo", "org": "AS1 NTT"}
        )
        self.assertEqual(geo_out.country, "JP")
        self.assertEqual(geo_out.timezone, "Asia/Tokyo")
        self.assertEqual(geo_out.org, "AS1 NTT")

    def test_ipapi_shape_uses_country_code(self):
        geo_out = normalize_geo_response(
            {"ip": "5.6.7.8", "country_code": "de", "region": "Berlin",
             "timezone": "Europe/Berlin"}
        )
        self.assertEqual(geo_out.country, "DE")  # upper-cased

    def test_ipwho_shape_nested_timezone(self):
        geo_out = normalize_geo_response(
            {"ip": "9.9.9.9", "country_code": "BR", "timezone": {"id": "America/Sao_Paulo"}}
        )
        self.assertEqual(geo_out.timezone, "America/Sao_Paulo")

    def test_ipwho_connection_org(self):
        geo_out = normalize_geo_response({"country": "US", "connection": {"org": "Comcast"}})
        self.assertEqual(geo_out.org, "Comcast")

    def test_garbage_returns_empty(self):
        for bad in (None, "", [], 42, {"country": None}):
            self.assertFalse(normalize_geo_response(bad).known)


class TraceParsingTests(unittest.TestCase):
    def test_parses_loc_and_ip(self):
        body = "fl=123f\nip=1.2.3.4\nloc=SG\ntls=TLSv1.3\n"
        parsed = geo.resolver._parse_trace(body)
        self.assertEqual(parsed.country, "SG")
        self.assertEqual(parsed.ip, "1.2.3.4")

    def test_missing_loc_returns_empty(self):
        self.assertFalse(geo.resolver._parse_trace("ip=1.2.3.4\ntls=TLSv1.3\n").known)


class EndpointOrderTests(unittest.TestCase):
    def test_trace_endpoint_is_first(self):
        # OpenAI sits behind Cloudflare, so a success on cdn-cgi/trace doubles as
        # a reachability signal — and it is plain text, so no JSON parsing and no
        # third-party rate limit. It must stay first.
        self.assertEqual(GEO_ENDPOINTS[0][0], "https://cloudflare.com/cdn-cgi/trace")
        self.assertEqual(GEO_ENDPOINTS[0][1], "trace")

    def test_all_endpoints_declare_a_kind(self):
        for url, kind in GEO_ENDPOINTS:
            self.assertTrue(url.startswith("http"), url)
            self.assertIn(kind, {"trace", "json"})


class ResolveTests(unittest.TestCase):
    def test_hint_short_circuits_probe(self):
        # The hint is free; making it always probe would put a network
        # round-trip on the registration hot path for no reason.
        probe = _FakeProbe(ProxyGeo(country="BR", source="probe"))
        resolver = _resolver(probe=probe)
        out = resolver.resolve("http://p:1", hint="JP")
        self.assertEqual(out.country, "JP")
        self.assertEqual(probe.calls, 0)

    def test_missing_hint_probes(self):
        probe = _FakeProbe(ProxyGeo(country="BR", timezone="America/Sao_Paulo"))
        resolver = _resolver(probe=probe)
        out = resolver.resolve("http://p:1")
        self.assertEqual(out.country, "BR")
        self.assertEqual(out.timezone, "America/Sao_Paulo")
        self.assertEqual(probe.calls, 1)

    def test_need_timezone_probes_even_with_hint(self):
        # A country alone is not enough to set a browser clock.
        probe = _FakeProbe(ProxyGeo(country="JP", timezone="Asia/Tokyo"))
        resolver = _resolver(probe=probe)
        out = resolver.resolve("http://p:1", hint="JP", need_timezone=True)
        self.assertEqual(out.timezone, "Asia/Tokyo")
        self.assertEqual(probe.calls, 1)

    def test_verify_hint_lets_measurement_win_and_warns(self):
        probe = _FakeProbe(ProxyGeo(country="BR"))
        resolver = _resolver(probe=probe, verify_hint=True)
        with self.assertLogs("sms_tool.geo.resolver", level="WARNING") as captured:
            out = resolver.resolve("http://p:1", hint="JP")
        self.assertEqual(out.country, "BR")
        self.assertIn("disagrees", "\n".join(captured.output))

    def test_raising_probe_degrades_to_unknown(self):
        # Geo is advisory. A dead proxy must not break registration.
        probe = _RaisingProbe()
        resolver = _resolver(probe=probe)
        self.assertFalse(resolver.resolve("http://p:1").known)

    def test_raising_probe_falls_back_to_hint(self):
        resolver = _resolver(probe=_RaisingProbe())
        out = resolver.resolve("http://p:1", hint="JP")
        self.assertEqual(out.country, "JP")

    def test_disabled_or_no_proxy_never_probes(self):
        probe = _FakeProbe(ProxyGeo(country="BR"))
        for resolver in (_resolver(enabled=False, probe=probe), _resolver(probe=probe)):
            self.assertFalse(resolver.resolve(None).known)
            self.assertFalse(resolver.resolve("").known)
        self.assertEqual(probe.calls, 0)

    def test_positive_result_is_cached(self):
        probe = _FakeProbe(ProxyGeo(country="JP"))
        resolver = _resolver(probe=probe)
        self.assertEqual(resolver.resolve("http://p:1").country, "JP")
        self.assertEqual(resolver.resolve("http://p:1").country, "JP")
        self.assertEqual(probe.calls, 1)

    def test_cached_answer_is_labelled_cache(self):
        resolver = _resolver(probe=_FakeProbe(ProxyGeo(country="JP")))
        resolver.resolve("http://p:1")
        self.assertEqual(resolver.resolve("http://p:1").source, "cache")

    def test_failure_expires_instead_of_poisoning_the_process(self):
        # The old code cached failures permanently: one network hiccup disabled
        # geo detection for the rest of the process. NEGATIVE_TTL is the fix.
        calls: list[int] = []

        def flaky(proxy, *, timeout, endpoints, need_timezone):
            calls.append(1)
            if len(calls) == 1:
                return ProxyGeo()
            return ProxyGeo(country="JP")

        resolver = GeoResolver(probe=flaky, negative_ttl=0.0, positive_ttl=10_000.0)
        self.assertFalse(resolver.resolve("http://p:1").known)
        self.assertEqual(resolver.resolve("http://p:1").country, "JP")
        self.assertEqual(len(calls), 2)

    def test_ttl_constants_are_sane(self):
        self.assertGreater(POSITIVE_TTL, 0)
        self.assertGreater(NEGATIVE_TTL, 0)
        # A failure must never be cached as long as a success.
        self.assertLess(NEGATIVE_TTL, POSITIVE_TTL)

    def test_proxy_spellings_share_one_cache_entry(self):
        # Pools are supplied as ``host:port:user:pass`` in places; without
        # normalization every spelling re-probes.
        probe = _FakeProbe(ProxyGeo(country="JP"))
        resolver = _resolver(probe=probe)
        resolver.resolve("http://user:pw@1.2.3.4:8080")
        resolver.resolve("1.2.3.4:8080:user:pw")
        self.assertEqual(probe.calls, 1)

    def test_invalidate_drops_entry(self):
        probe = _FakeProbe(ProxyGeo(country="JP"))
        resolver = _resolver(probe=probe)
        resolver.resolve("http://p:1")
        resolver.invalidate("http://p:1")
        resolver.resolve("http://p:1")
        self.assertEqual(probe.calls, 2)


class RememberTests(unittest.TestCase):
    """The fix for the discarded preflight measurement."""

    def test_published_measurement_is_reused_without_probing(self):
        resolver = _resolver(probe=_RaisingProbe())
        resolver.remember("http://p:1", ProxyGeo(country="JP", timezone="Asia/Tokyo"))
        out = resolver.resolve("http://p:1")
        self.assertEqual(out.country, "JP")
        self.assertEqual(out.timezone, "Asia/Tokyo")

    def test_publish_does_not_downgrade_a_richer_answer(self):
        resolver = _resolver(probe=_FakeProbe(ProxyGeo(country="JP", timezone="Asia/Tokyo")))
        resolver.resolve("http://p:1")
        # A country-only measurement must not blank the timezone we already have.
        resolver.remember("http://p:1", ProxyGeo(country="JP"))
        self.assertEqual(resolver.resolve("http://p:1").timezone, "Asia/Tokyo")

    def test_publish_ignores_unknown_geo(self):
        resolver = _resolver(probe=_FakeProbe(ProxyGeo(country="JP")))
        resolver.remember("http://p:1", ProxyGeo())
        out = resolver.resolve("http://p:1")
        self.assertEqual(out.country, "JP")  # probe still ran -> nothing cached

    def test_publish_ignores_empty_proxy(self):
        resolver = _resolver(probe=_FakeProbe(ProxyGeo(country="JP")))
        resolver.remember("", ProxyGeo(country="BR"))
        resolver.remember(None, ProxyGeo(country="BR"))
        self.assertEqual(len(resolver._cache), 0)


class SharedResolverTests(unittest.TestCase):
    def setUp(self):
        reset_shared_geo_resolver()

    def tearDown(self):
        reset_shared_geo_resolver()

    def test_shared_resolver_is_one_instance(self):
        self.assertIs(geo.shared_geo_resolver(), geo.shared_geo_resolver())

    def test_module_helper_routes_through_shared_cache(self):
        geo.shared_geo_resolver().remember("http://p:1", ProxyGeo(country="SG"))
        self.assertEqual(geo.resolve_proxy_geo("http://p:1").country, "SG")


class CrossCallerIntegrationTests(unittest.TestCase):
    """A measurement taken by one caller is visible to the others.

    This is the whole point of P0-1: the payment preflight measures the egress
    country, and the browser fingerprint pool must reuse that answer instead of
    guessing again from the credential template.
    """

    def setUp(self):
        reset_shared_geo_resolver()

    def tearDown(self):
        reset_shared_geo_resolver()

    def test_preflight_measurement_reaches_the_browser_pool(self):
        proxy = "http://user:pw@geo.example:8080"
        geo.remember_proxy_geo(proxy, ProxyGeo(country="JP", timezone="Asia/Tokyo"))
        # The probe seam raising proves the answer came from the shared cache.
        with patch(
            "sms_tool.browser_fingerprint_pool._query_geo_endpoints",
            side_effect=AssertionError("must not probe"),
        ):
            out = detect_proxy_exit_geo(proxy)
        self.assertEqual(out["country"], "JP")
        self.assertEqual(out["timezone"], "Asia/Tokyo")

    def test_paypal_preflight_publishes_to_shared_cache(self):
        from sms_tool import paypal_proxy

        result = paypal_proxy.ProxyProbeResult(
            ok=True,
            stage="proxy",
            expected_country="JP",
            ip="1.2.3.4",
            country_code="JP",
            country="Japan",
            region="Tokyo",
        )
        paypal_proxy._publish_probe_geo("http://user:pw@geo.example:8080", result)
        self.assertEqual(
            geo.shared_geo_resolver()
            .resolve("http://user:pw@geo.example:8080")
            .country,
            "JP",
        )

    def test_failed_preflight_publishes_nothing(self):
        from sms_tool import paypal_proxy

        result = paypal_proxy.ProxyProbeResult(
            ok=False, stage="proxy", error="proxy_probe_failed:timeout"
        )
        paypal_proxy._publish_probe_geo("http://user:pw@geo.example:8080", result)
        self.assertEqual(len(geo.shared_geo_resolver()._cache), 0)


class ProtocolPathGeoTests(unittest.TestCase):
    """The protocol path must not keep a US clock on a non-US egress.

    ``FingerprintPool._with_geo`` used to call ``infer_proxy_country`` alone.
    A proxy whose credential carries no region token resolved to ``""``, so the
    profile silently kept ``America/New_York`` + ``en-US`` — a Brazilian exit IP
    reporting a US Eastern clock.
    """

    def setUp(self):
        reset_shared_geo_resolver()

    def tearDown(self):
        reset_shared_geo_resolver()

    def _profile(self, proxy):
        from sms_tool.fingerprint_pool import FingerprintPool

        return FingerprintPool.from_config({}).next(proxy)

    def test_uncurated_country_gets_the_measured_clock(self):
        # BR is not in ``_GEO_PROFILES`` (only US/CA/GB/DE/FR/JP/SG/AU are).
        proxy = "socks5h://user:pw@br.example.net:1080"
        geo.remember_proxy_geo(
            proxy, ProxyGeo(country="BR", timezone="America/Sao_Paulo")
        )
        with patch(
            "sms_tool.browser_fingerprint_pool._query_geo_endpoints",
            side_effect=AssertionError("must not probe"),
        ):
            out = self._profile(proxy)
        self.assertEqual(out.country, "BR")
        self.assertEqual(out.timezone, "America/Sao_Paulo")

    def test_unknown_geo_leaves_the_profile_alone(self):
        # Nothing measured and nothing inferred -> keep the profile defaults
        # rather than inventing a country.
        resolver = geo.shared_geo_resolver()
        resolver.enabled = False
        out = self._profile("socks5h://user:pw@x.example.net:1080")
        self.assertEqual(out.timezone, "America/New_York")

    def test_curated_country_needs_no_probe(self):
        # JP is in ``_GEO_PROFILES``, so the template hint answers it outright
        # and no measurement is paid for.
        resolver = geo.shared_geo_resolver()
        resolver.probe = _RaisingProbe()
        proxy = "http://customer-x-JP-sess1:pw@kookeey.example:8080"
        out = self._profile(proxy)
        self.assertEqual(out.country, "JP")
        self.assertEqual(out.timezone, "Asia/Tokyo")
        self.assertEqual(out.lang, "ja-JP")

    def test_no_proxy_returns_the_plain_profile(self):
        # Without an egress there is nothing to align to; the profile's own
        # (US) defaults stand.
        out = self._profile(None)
        self.assertEqual(out.country, "US")
        self.assertEqual(out.timezone, "America/New_York")
        self.assertEqual(out.lang, "en-US")


class ProbeExitGeoTests(unittest.TestCase):
    def test_no_proxy_returns_empty(self):
        self.assertFalse(probe_exit_geo(None).known)
        self.assertFalse(probe_exit_geo("").known)

    def test_trace_endpoint_wins_over_json(self):
        def fake_get(url, proxy, timeout):
            if url.endswith("/cdn-cgi/trace"):
                return 200, "ip=1.2.3.4\nloc=DE\n"
            raise AssertionError("json endpoint should not be reached")

        with patch.object(geo.resolver, "_http_get", fake_get):
            out = probe_exit_geo("http://p:1")
        self.assertEqual(out.country, "DE")

    def test_need_timezone_keeps_walking_past_country_only_answer(self):
        def fake_get(url, proxy, timeout):
            if url.endswith("/cdn-cgi/trace"):
                return 200, "ip=1.2.3.4\nloc=DE\n"
            return 200, '{"country_code":"DE","timezone":"Europe/Berlin","ip":"1.2.3.4"}'

        with patch.object(geo.resolver, "_http_get", fake_get):
            out = probe_exit_geo("http://p:1", need_timezone=True)
        self.assertEqual(out.country, "DE")
        self.assertEqual(out.timezone, "Europe/Berlin")

    def test_need_timezone_falls_back_to_country_only(self):
        def fake_get(url, proxy, timeout):
            if url.endswith("/cdn-cgi/trace"):
                return 200, "ip=1.2.3.4\nloc=DE\n"
            return 0, ""

        with patch.object(geo.resolver, "_http_get", fake_get):
            out = probe_exit_geo("http://p:1", need_timezone=True)
        self.assertEqual(out.country, "DE")
        self.assertEqual(out.timezone, "")

    def test_all_endpoints_failing_returns_empty(self):
        with patch.object(geo.resolver, "_http_get", lambda *a, **k: (0, "")):
            self.assertFalse(probe_exit_geo("http://p:1").known)

    def test_all_endpoints_failing_stays_quiet_at_warning_level(self):
        # A dead geo endpoint is routine (a proxy may simply block these hosts);
        # it must not spam the operator with warnings.
        logger = logging.getLogger("sms_tool.geo.resolver")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with patch.object(geo.resolver, "_http_get", lambda *a, **k: (0, "")):
                self.assertFalse(probe_exit_geo("http://p:1").known)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        loud = [r for r in records if r.levelno >= logging.WARNING]
        self.assertFalse(loud, f"routine geo miss must not warn, got: {loud}")


if __name__ == "__main__":
    unittest.main()
