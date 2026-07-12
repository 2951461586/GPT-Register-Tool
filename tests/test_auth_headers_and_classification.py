import unittest

from sms_tool.auth_headers import openai_auth_headers
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

    def test_error_classification_prioritizes_account_over_timeout_substring(self):
        self.assertEqual(classify_error("outlook otp timeout"), "account")
        self.assertEqual(classify_error("[WinError 10060] connection timeout via proxy"), "network")
        self.assertEqual(classify_error({"error": "account_deactivated", "body": "timeout"}), "account")


if __name__ == "__main__":
    unittest.main()
