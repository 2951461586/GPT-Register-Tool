import unittest
from unittest.mock import patch

from sms_tool import auth_headers
from sms_tool.auth_headers import AUTH_IMPERSONATE, DEFAULT_SEC_CH_UA, DEFAULT_USER_AGENT, openai_auth_headers
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

    def test_sentinel_extraction_failure_is_retryable_network_error(self):
        self.assertEqual(classify_error("sentinel_extract_failed"), "network")


if __name__ == "__main__":
    unittest.main()
