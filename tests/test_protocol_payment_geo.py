"""Tests for services/protocol-payment/common/geo.py.

The pure helpers are asserted directly. The stateful lookup functions are driven
through injected fakes — that is the point of the design: no extractor module
state is touched, so the shared logic can be tested without standing up blik.
"""

import sys
import unittest
from pathlib import Path

# Load as part of the ``common`` package so ``from . import endpoints`` resolves.
_COMMON_DIR = (
    Path(__file__).resolve().parents[1] / "services" / "protocol-payment"
)
if str(_COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(_COMMON_DIR))

import common.endpoints  # noqa: E402,F401  (registers the package)
from common import geo as GEO  # noqa: E402


def _env_bool(name, default=False):
    return default


def _env_int(name, default, minimum=1):
    return default


def _redact(text):
    return text


class PureHelperTests(unittest.TestCase):
    def test_clean_country_code(self):
        self.assertEqual(GEO.clean_country_code(" pl "), "PL")
        self.assertEqual(GEO.clean_country_code("usa"), "US")
        self.assertEqual(GEO.clean_country_code(""), "")
        self.assertEqual(GEO.clean_country_code("p1l!"), "PL")

    def test_format_expected_countries_is_sorted(self):
        self.assertEqual(GEO.format_expected_countries({"PL", "DE", "NL"}), "DE,NL,PL")

    def test_parse_geo_country_ip_api(self):
        country, asn = GEO.parse_geo_country("ip-api", {"status": "success", "countryCode": "pl", "as": "AS123"})
        self.assertEqual((country, asn), ("PL", "AS123"))
        self.assertEqual(GEO.parse_geo_country("ip-api", {"status": "fail", "message": "bad"}), ("", "bad"))

    def test_parse_geo_country_ipwho(self):
        payload = {"success": True, "country_code": "NL", "connection": {"asn": 42}}
        self.assertEqual(GEO.parse_geo_country("ipwho", payload), ("NL", "42"))
        self.assertEqual(GEO.parse_geo_country("ipwho", {"success": False, "message": "x"}), ("", "x"))

    def test_parse_geo_country_ipapi(self):
        self.assertEqual(GEO.parse_geo_country("ipapi", {"country_code": "DE", "org": "ISP"}), ("DE", "ISP"))
        self.assertEqual(GEO.parse_geo_country("ipapi", {"error": True, "reason": "rate"}), ("", "rate"))

    def test_parse_geo_country_unknown_source(self):
        self.assertEqual(GEO.parse_geo_country("nope", {}), ("", ""))

    def test_target_probe_urls_checkout_vs_other(self):
        self.assertEqual([u for _, u in GEO.target_probe_urls("checkout")], ["https://chatgpt.com/"])
        self.assertEqual(
            [u for _, u in GEO.target_probe_urls("provider")],
            ["https://chatgpt.com/", "https://api.stripe.com/"],
        )

    def test_target_response_error(self):
        class Resp:
            def __init__(self, code, headers=None):
                self.status_code = code
                self.headers = headers or {}

        self.assertEqual(GEO.target_response_error(Resp(200)), "")
        self.assertEqual(GEO.target_response_error(Resp(407)), "HTTP_407:proxy-auth")
        self.assertEqual(GEO.target_response_error(Resp(500)), "HTTP_500")
        self.assertEqual(
            GEO.target_response_error(Resp(403, {"x-response-origin": "proxy-server"})),
            "HTTP_403:proxy-server",
        )


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    """Records requested urls; serves queued responses keyed by url substring."""

    def __init__(self, responses):
        self._responses = responses
        self.headers = {}
        self.requested = []

    def get(self, url, timeout=None, allow_redirects=True):
        self.requested.append(url)
        for key, resp in self._responses.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _FakeResponse(404, {})


class LookupProxyCountryTests(unittest.TestCase):
    def _make(self, responses, record=None):
        saved = []
        session = _FakeSession(responses)
        record = record if record is not None else {}
        kwargs = dict(
            record=record,
            save_state=lambda: saved.append(True),
            new_session=lambda proxy, use_pre_proxy=False: session,
            env_bool=_env_bool,
            env_int=_env_int,
            redact=_redact,
            env_prefix="TEST",
        )
        return record, saved, session, kwargs

    def test_success_writes_cache_and_returns_source(self):
        record, saved, session, kwargs = self._make(
            {"ip-api": _FakeResponse(200, {"status": "success", "countryCode": "pl", "as": "AS1"})}
        )
        country, asn, source = GEO.lookup_proxy_country("seed", "http://p", **kwargs)
        self.assertEqual((country, asn, source), ("PL", "AS1", "ip-api"))
        self.assertEqual(record["country"], "PL")
        self.assertTrue(saved)

    def test_cache_hit_short_circuits_network(self):
        record = {"country": "NL", "country_as": "AS9", "country_checked_at": 9999999999, "country_pre_proxy": False}
        import time
        record["country_checked_at"] = int(time.time())
        _r, saved, session, kwargs = self._make({}, record=record)
        country, asn, source = GEO.lookup_proxy_country("seed", "http://p", **kwargs)
        self.assertEqual((country, asn, source), ("NL", "AS9", "cache"))
        self.assertEqual(session.requested, [])

    def test_all_endpoints_fail_records_error(self):
        record, saved, session, kwargs = self._make({})
        country, last_error, source = GEO.lookup_proxy_country("seed", "http://p", **kwargs)
        self.assertEqual(country, "")
        self.assertEqual(source, "error")
        self.assertIn("country_error", record)


class LookupProxyTargetsTests(unittest.TestCase):
    def test_reachable_when_no_error(self):
        session = _FakeSession({"chatgpt": _FakeResponse(200)})
        record = {}
        ok, reason = GEO.lookup_proxy_targets(
            "seed", "http://p",
            record=record, save_state=lambda: None,
            new_session=lambda proxy, use_pre_proxy=True: session,
            env_bool=_env_bool, env_int=_env_int, redact=_redact,
            env_prefix="TEST", user_agent="UA",
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertTrue(record["target_ok"])

    def test_unreachable_on_5xx(self):
        session = _FakeSession({"chatgpt": _FakeResponse(503)})
        record = {}
        ok, reason = GEO.lookup_proxy_targets(
            "seed", "http://p",
            record=record, save_state=lambda: None,
            new_session=lambda proxy, use_pre_proxy=True: session,
            env_bool=_env_bool, env_int=_env_int, redact=_redact,
            env_prefix="TEST", user_agent="UA",
        )
        self.assertFalse(ok)
        self.assertIn("503", reason)


class EnsureTests(unittest.TestCase):
    def test_ensure_country_raises_on_mismatch(self):
        with self.assertRaises(RuntimeError):
            GEO.ensure_proxy_country(
                "seed", "http://p",
                lookup_country=lambda g, p: ("US", "", "ip-api"),
                expected_countries=lambda g: {"PL"},
                env_bool=lambda n, d=True: True,
                log=lambda *a, **k: None,
                label=lambda p: p,
                remove_failed=lambda g, p, r: None,
                env_prefix="TEST",
            )

    def test_ensure_country_passes_on_match(self):
        GEO.ensure_proxy_country(
            "seed", "http://p",
            lookup_country=lambda g, p: ("PL", "", "ip-api"),
            expected_countries=lambda g: {"PL"},
            env_bool=lambda n, d=True: True,
            log=lambda *a, **k: None,
            label=lambda p: p,
            remove_failed=lambda g, p, r: None,
            env_prefix="TEST",
        )

    def test_ensure_targets_raises_on_unreachable(self):
        with self.assertRaises(RuntimeError):
            GEO.ensure_proxy_targets(
                "seed", "http://p",
                lookup_targets=lambda g, p: (False, "HTTP_503"),
                env_bool=lambda n, d=True: True,
                log=lambda *a, **k: None,
                label=lambda p: p,
                env_prefix="TEST",
            )


class ExpectedCountriesTests(unittest.TestCase):
    def test_checkout_uses_single_country(self):
        import os
        os.environ.pop("TEST_CHECKOUT_PROXY_COUNTRY", None)
        result = GEO.expected_proxy_countries(
            "checkout", env_prefix="TEST",
            default_checkout_country=lambda: "PL",
            default_country=lambda: "NL",
            default_provider_countries=lambda: "NL,BE",
        )
        self.assertEqual(result, {"PL"})

    def test_provider_parses_list(self):
        import os
        os.environ["TEST_PROVIDER_PROXY_COUNTRIES"] = "nl, be ;de"
        try:
            result = GEO.expected_proxy_countries(
                "provider", env_prefix="TEST",
                default_checkout_country=lambda: "PL",
                default_country=lambda: "NL",
                default_provider_countries=lambda: "NL",
            )
            self.assertEqual(result, {"NL", "BE", "DE"})
        finally:
            os.environ.pop("TEST_PROVIDER_PROXY_COUNTRIES", None)


if __name__ == "__main__":
    unittest.main()

