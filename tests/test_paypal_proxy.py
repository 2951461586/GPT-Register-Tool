import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import gen_pp_link
from sms_tool import paypal_proxy


class PayPalProxyTests(unittest.TestCase):
    def test_probe_detects_socks5h_when_bare_entry_defaulted_to_http(self):
        seen = []

        def fake_probe(value, expected, stage, _timeout):
            seen.append(value)
            return paypal_proxy.ProxyProbeResult(
                ok=value.startswith("socks5h://"),
                stage=stage,
                expected_country=expected,
                country_code=expected if value.startswith("socks5h://") else "",
                error="proxy_probe_failed" if value.startswith("http://") else "",
            )

        with patch.object(paypal_proxy, "_probe_proxy_network", side_effect=fake_probe):
            result = paypal_proxy.probe_proxy("proxy.example:1080:user:pass", "US")

        self.assertTrue(result.ok)
        self.assertEqual(result.scheme, "socks5h")
        self.assertEqual(
            [value.split("://", 1)[0] for value in seen],
            ["http", "socks5h"],
        )

    def test_proxy_probe_error_redacts_authenticated_proxy(self):
        class FailingSession:
            trust_env = True
            proxies = {}

            def get(self, *_args, **_kwargs):
                raise RuntimeError("failed via http://private-user:private-pass@proxy.example:443")

        with patch.object(paypal_proxy.requests, "Session", return_value=FailingSession()):
            result = paypal_proxy.probe_proxy("http://user:pass@proxy.example:443", "JP")

        self.assertNotIn("private-user", result.error)
        self.assertNotIn("private-pass", result.error)
        self.assertIn("http://***:***@proxy.example:443", result.error)

    def test_payment_pool_falls_back_and_rotates_to_target_country(self):
        pool = [
            "http://first-region-JP-sid-Ab12Cd34-t-5:secret@sg.cliproxy.io:443",
            "http://second-region-JP-sid-Ef56Gh78-t-10:secret@as.zooproxy.com:443",
        ]

        def fake_probe(proxy, expected_country="", stage="proxy", timeout=12):
            return paypal_proxy.ProxyProbeResult(
                ok="as.zooproxy.com" in proxy,
                stage=stage,
                expected_country=expected_country,
                country_code=expected_country if "as.zooproxy.com" in proxy else "",
                error="timeout" if "sg.cliproxy.io" in proxy else "",
            )

        with patch.object(paypal_proxy, "probe_proxy", side_effect=fake_probe):
            selected, attempts = paypal_proxy.select_proxy_from_pool(pool, "VN", "momo")

        self.assertIn("as.zooproxy.com", selected)
        self.assertIn("region-VN", selected)
        self.assertEqual([item["ok"] for item in attempts], [False, True])

    def test_rotates_cliproxy_country_and_session(self):
        original = "socks5://user-region-US-sid-Ab12Cd34-t-5:secret@us.cliproxy.io:443"

        rotated = paypal_proxy.rotate_proxy_session(original, "GB")

        self.assertIn("region-GB", rotated)
        self.assertNotIn("sid-Ab12Cd34", rotated)
        self.assertEqual(paypal_proxy.infer_proxy_country(rotated), "GB")

    def test_rotates_kookeey_password_country_and_numeric_session(self):
        original = "http://account:base-US-54055465-5m@gate.kookeey.info:1000"

        rotated = paypal_proxy.rotate_proxy_session(original, "TR")

        self.assertIn("-TR-", rotated)
        self.assertNotIn("54055465", rotated)
        self.assertEqual(paypal_proxy.infer_proxy_country(rotated), "TR")
        self.assertEqual(
            paypal_proxy.redact_proxy_url(rotated),
            "http://***:***@gate.kookeey.info:1000",
        )

    def test_state_prefers_successful_pair_and_never_persists_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            state = paypal_proxy.PayPalProxyState(path, fail_skip_after=1, fail_cooldown_seconds=600)
            checkout = "http://checkout-user:checkout-pass@checkout.example:1000"
            provider_a = "http://provider-a:secret-a@provider.example:1000"
            provider_b = "http://provider-b:secret-b@provider.example:1000"
            state.record_result("provider", provider_a, False, "timeout", "US")
            state.record_result("provider", provider_b, True, country="US")
            state.record_pair_result(checkout, provider_b, provider_b, True)

            ranked = state.rank("provider", [provider_a, provider_b], country="US", checkout_proxy=checkout)

            self.assertEqual(ranked, [provider_b])
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("checkout-pass", raw)
            self.assertNotIn("secret-a", raw)
            self.assertNotIn("secret-b", raw)

    def test_cached_country_mismatch_is_not_replayed_for_that_country(self):
        """A mismatch recorded against another country must not fail this one.

        The same proxy template is probed for different countries across
        methods and stages. Replaying an old "expected KR, got JP" verdict for a
        request that now expects JP would reject a working proxy.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            state = paypal_proxy.PayPalProxyState(Path(temp_dir) / "state.json")
            proxy = "http://user-region-JP:secret@proxy.example:1000"
            state.record_probe(
                proxy,
                paypal_proxy.ProxyProbeResult(
                    ok=False,
                    stage="approve",
                    expected_country="KR",
                    ip="1.2.3.4",
                    country_code="JP",
                    country="Japan",
                    region="Tokyo",
                    error="country_mismatch:JP",
                ),
            )

            # The ask now matches the exit that was actually reached -> re-probe.
            self.assertIsNone(state.cached_probe(proxy, "JP", "approve"))
            # A different ask is still answered from cache (proxy stays skipped).
            skipped = state.cached_probe(proxy, "KR", "approve")
            self.assertIsNotNone(skipped)
            self.assertFalse(skipped.ok)

    def test_cached_transport_failure_still_skips_dead_proxy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = paypal_proxy.PayPalProxyState(Path(temp_dir) / "state.json")
            proxy = "http://user-region-JP:secret@dead.example:1000"
            state.record_probe(
                proxy,
                paypal_proxy.ProxyProbeResult(
                    ok=False,
                    stage="approve",
                    expected_country="JP",
                    ip="",
                    country_code="",
                    country="",
                    region="",
                    error="proxy_probe_failed:HTTP 407",
                ),
            )

            cached = state.cached_probe(proxy, "JP", "approve")
            self.assertIsNotNone(cached)
            self.assertFalse(cached.ok)

    def test_zero_cache_skips_known_nonzero_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = paypal_proxy.PayPalProxyState(Path(temp_dir) / "state.json")
            bad = "http://bad:secret@proxy.example:1000"
            good = "http://good:secret@proxy.example:1000"
            state.record_zero_result(bad, "US", 2000)
            state.record_zero_result(good, "US", 0)

            ranked = state.rank("checkout", [bad, good], country="US")

            self.assertEqual(ranked, [good])

    def test_proxy_probe_reports_country_mismatch(self):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "success", "query": "1.2.3.4", "countryCode": "GB", "country": "United Kingdom"}

        class FakeSession:
            trust_env = True
            proxies = {}

            def get(self, *args, **kwargs):
                return FakeResponse()

        with patch.object(paypal_proxy.requests, "Session", return_value=FakeSession()):
            result = paypal_proxy.probe_proxy("http://user:pass@proxy:1000", "US", "checkout")

        self.assertFalse(result.ok)
        self.assertEqual(result.country_code, "GB")
        self.assertIn("country_mismatch", result.error)

    def test_config_keeps_confirm_and_provider_stages_distinct(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = {
                "paypal": {
                    "stage_proxies": {
                        "checkout": "http://checkout",
                        "provider": "http://provider",
                        "stripe_init": "http://init",
                        "payment_method": "http://pm",
                        "confirm": "http://confirm",
                        "approve": "http://approve",
                    },
                    "proxy_health": {"state_file": str(Path(temp_dir) / "state.json")},
                }
            }
            gen_pp_link._PAYPAL_PROXY_STATE_CACHE.clear()

            stages = gen_pp_link._proxies_from_config(cfg, checkout_country="US", target_country="GB")

        self.assertEqual(stages["provider"], "http://provider")
        self.assertEqual(stages["stripe_init"], "http://init")
        self.assertEqual(stages["payment_method"], "http://pm")
        self.assertEqual(stages["confirm"], "http://confirm")
        self.assertEqual(stages["approve"], "http://approve")

    def test_generate_passes_each_segment_to_extractor(self):
        seen = {}

        class FakeExtractor:
            def __init__(self, **kwargs):
                seen.update(kwargs)

            def extract(self):
                return {
                    "ok": True,
                    "url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                    "ba_token": "BA-test",
                    "link_type": "paypal_ba_approve",
                }

        cfg = {
            "paypal": {
                "link_generation_type": "paypal_direct_zero_due",
                "stage_proxies": {
                    "checkout": "http://checkout",
                    "provider": "http://provider",
                    "stripe_init": "http://init",
                    "payment_method": "http://pm",
                    "confirm": "http://confirm",
                    "approve": "http://approve",
                },
                "preflight_proxy_check": False,
            }
        }
        with patch.object(gen_pp_link, "_load_json", return_value=cfg):
            with patch.object(gen_pp_link, "PPLinkExtractor", FakeExtractor):
                result = gen_pp_link.generate_pp_link("at", target_country="US", checkout_country="US")

        self.assertTrue(result["ok"])
        self.assertEqual(seen["checkout_proxy"], "http://checkout")
        self.assertEqual(seen["provider_proxy"], "http://provider")
        self.assertEqual(seen["stripe_init_proxy"], "http://init")
        self.assertEqual(seen["payment_method_proxy"], "http://pm")
        self.assertEqual(seen["confirm_proxy"], "http://confirm")
        self.assertEqual(seen["approve_proxy"], "http://approve")

    def test_confirm_diagnostics_extracts_stripe_fields(self):
        class FakeResponse:
            status_code = 402
            text = "declined"

            def json(self):
                return {
                    "error": {"type": "card_error", "code": "checkout_confirm_error", "message": "blocked"},
                    "submission_attempt": {"state": "failed", "reason": "provider_declined"},
                }

        diagnostics = gen_pp_link.stripe_confirm_error_diagnostics(
            FakeResponse(),
            "cs_test",
            "pm_test",
            {"init_checksum": "checksum", "total_summary": {"due": 0}},
        )

        self.assertIn("error_code=checkout_confirm_error", diagnostics)
        self.assertIn("submission_state=failed", diagnostics)
        self.assertIn("submission_reason=provider_declined", diagnostics)


if __name__ == "__main__":
    unittest.main()
