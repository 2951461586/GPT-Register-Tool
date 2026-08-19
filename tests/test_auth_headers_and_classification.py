import unittest
from unittest.mock import patch

from sms_tool import auth_headers
from sms_tool.auth_headers import (
    AUTH_IMPERSONATE,
    DEFAULT_SEC_CH_UA,
    DEFAULT_USER_AGENT,
    openai_auth_headers,
    nextauth_headers,
    chatgpt_headers,
    set_fingerprint_device,
    set_fingerprint_geo,
    sentinel_fingerprint,
)
from sms_tool.error_classification import classify_error


class AuthHeadersAndClassificationTests(unittest.TestCase):
    def test_auth_headers_include_device_sentinel_and_trace(self):
        headers = openai_auth_headers(
            "did-1",
            referer="https://auth.openai.com/create-account",
            sentinel={"sentinel_token": "sentinel", "sentinel_so_token": "so"},
            extra={"content-type": "application/json"},
        )

        self.assertEqual(headers["oai-device-id"], "did-1")
        self.assertEqual(headers["Origin"], "https://auth.openai.com")
        self.assertEqual(headers["openai-sentinel-token"], "sentinel")
        self.assertEqual(headers["openai-sentinel-so-token"], "so")
        self.assertIn("traceparent", headers)
        self.assertIn("x-datadog-trace-id", headers)

    def test_auth_headers_derive_origin_from_extra_referer(self):
        headers = openai_auth_headers(
            "did-2",
            extra={"Referer": "https://auth.openai.com/email-verification"},
        )

        self.assertEqual(headers["Origin"], "https://auth.openai.com")
        self.assertEqual(headers["Referer"], "https://auth.openai.com/email-verification")

    def test_geo_profile_changes_locale_and_timezone(self):
        set_fingerprint_geo("JP")
        fingerprint = sentinel_fingerprint()
        self.assertEqual(fingerprint["timezone"], "Asia/Tokyo")
        self.assertEqual(fingerprint["lang"], "ja-JP")
        set_fingerprint_geo("US")

    def test_header_families_have_distinct_protocol_fields(self):
        nextauth = nextauth_headers("did", session_id="sid")
        chat = chatgpt_headers("did", session_id="sid")
        self.assertNotIn("traceparent", nextauth)
        self.assertNotIn("oai-client-build-number", nextauth)
        self.assertEqual(chat["oai-client-build-number"], "8370486")
        self.assertEqual(chat["oai-session-id"], "sid")

    def test_auth_browser_fingerprint_versions_are_consistent(self):
        version = AUTH_IMPERSONATE.removeprefix("chrome")
        self.assertIn(f"Chrome/{version}.", DEFAULT_USER_AGENT)
        self.assertIn(f'v="{version}"', DEFAULT_SEC_CH_UA)

    def test_rotated_fingerprint_keeps_tls_and_client_hints_coherent(self):
        cfg = {"mode": "rotate", "profiles": ["chrome124", "chrome131"]}
        with patch.object(auth_headers, "_auth_fingerprint_config", return_value=cfg):
            first = auth_headers.select_auth_fingerprint(rotate=True)
            second = auth_headers.select_auth_fingerprint(rotate=True)
            headers = auth_headers.openai_auth_headers("did")

        self.assertNotEqual(first["name"], second["name"])
        version = second["impersonate"].removeprefix("chrome")
        self.assertIn(f"Chrome/{version}.", headers["User-Agent"])
        self.assertIn(f'v="{version}"', headers["sec-ch-ua"])
        auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = AUTH_IMPERSONATE

    def test_sentinel_fingerprint_matches_auth_profile(self):
        with patch.object(auth_headers, "current_auth_fingerprint", return_value={
            "name": "chrome146",
            "impersonate": "chrome146",
            "user_agent": "Mozilla/5.0 Chrome/146.0.0.0 Safari/537.36",
            "sec_ch_ua": '"Chromium";v="146"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        }):
            fingerprint = sentinel_fingerprint()
        self.assertEqual(fingerprint["impersonate"], "chrome146")
        self.assertIn("Chrome/146.", fingerprint["user_agent"])
        self.assertEqual(fingerprint["navigator_platform"], "Win32")

    def test_device_profile_is_deterministic_per_account_and_differs_across(self):
        # Same device id → identical hardware/display readings every time, so a
        # single account looks like one stable machine across relogin/recovery.
        hardware_keys = (
            "screen", "hardware_concurrency", "device_memory",
            "device_pixel_ratio", "js_heap_size_limit",
        )
        set_fingerprint_device("device-alpha")
        first = {key: sentinel_fingerprint()[key] for key in hardware_keys}
        set_fingerprint_device("device-alpha")
        second = {key: sentinel_fingerprint()[key] for key in hardware_keys}
        self.assertEqual(first, second)

        # A different account must not reuse the same device silhouette.
        distinct = set()
        for seed in ("device-beta", "device-gamma", "device-delta", "device-epsilon"):
            set_fingerprint_device(seed)
            profile = sentinel_fingerprint()
            distinct.add(tuple(profile[key] for key in hardware_keys))
        self.assertGreater(len(distinct), 1)

        # Values stay within the realistic desktop pools (deviceMemory capped 8).
        set_fingerprint_device("device-alpha")
        profile = sentinel_fingerprint()
        self.assertIn(profile["screen"], auth_headers._SCREEN_CHOICES)
        self.assertIn(profile["device_memory"], (4, 8))
        self.assertEqual(profile["max_touch_points"], 0)
        set_fingerprint_device("")

    def test_error_classification_prioritizes_account_over_timeout_substring(self):
        self.assertEqual(classify_error("outlook otp timeout"), "mailbox")
        self.assertEqual(classify_error("[WinError 10060] connection timeout via proxy"), "network")
        self.assertEqual(classify_error({"error": "account_deactivated", "body": "timeout"}), "account")

    def test_mailbox_timeout_is_not_classified_as_dropped_account(self):
        self.assertEqual(classify_error("email_otp_poll_timeout"), "mailbox")

    def test_invalid_auth_step_is_not_classified_as_dropped_account(self):
        self.assertEqual(classify_error("create_account_failed:invalid_auth_step"), "auth_state")

    def test_cloudflare_page_wins_over_generic_signup_auth_state(self):
        self.assertEqual(classify_error("signup_auth_state: 403 Just a moment..."), "network")

    def test_rate_limit_wins_over_generic_signup_auth_state(self):
        self.assertEqual(
            classify_error("signup_auth_state: status 429 rate_limit_exceeded Too many requests"),
            "rate_limit",
        )

    def test_sentinel_extraction_failure_is_retryable_network_error(self):
        self.assertEqual(classify_error("sentinel_extract_failed"), "network")


if __name__ == "__main__":
    unittest.main()
