import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import paypal_protocol


class PayPalProtocolTests(unittest.TestCase):
    def test_extracts_ba_and_ec_tokens(self):
        self.assertEqual(
            paypal_protocol.extract_ba_token(
                "https://www.paypal.com/agreements/approve?ba_token=BA-123_ABC-def"
            ),
            "BA-123_ABC-def",
        )
        self.assertEqual(
            paypal_protocol.extract_ec_token("<input value='EC-1ABCD234EFGH56789'>"),
            "EC-1ABCD234EFGH56789",
        )
        self.assertIsNone(paypal_protocol.extract_ba_token("https://pm-redirects.stripe.com/authorize/test"))
        self.assertIsNone(paypal_protocol.extract_ec_token("missing"))

    def test_extracts_paypal_approve_url_from_body_or_bare_token(self):
        self.assertEqual(
            paypal_protocol._extract_paypal_approve_url(
                r'{"url":"https:\/\/www.paypal.com\/agreements\/approve?ba_token=BA-BODY_123.456-789\u0026x=1"}'
            ),
            "https://www.paypal.com/agreements/approve?ba_token=BA-BODY_123.456-789&x=1",
        )
        self.assertEqual(
            paypal_protocol._extract_paypal_approve_url("next=paypal&ba_token=BA-ONLY_123.456-789"),
            "https://www.paypal.com/agreements/approve?ba_token=BA-ONLY_123.456-789",
        )

    def test_follows_stripe_redirect_location_then_body(self):
        class FakeSession:
            def __init__(self):
                self.urls = []

            def get(self, url, **kwargs):
                self.urls.append(url)
                if len(self.urls) == 1:
                    return SimpleNamespace(
                        status_code=302,
                        headers={"Location": "https://pm-redirects.stripe.com/authorize/next"},
                        text="",
                        url=url,
                    )
                return SimpleNamespace(
                    status_code=200,
                    headers={},
                    text=r'{"url":"https:\/\/www.paypal.com\/agreements\/approve?ba_token=BA-FOLLOW_123.456-789\u0026x=1"}',
                    url=url,
                )

        session = FakeSession()
        logs = []
        with patch.object(paypal_protocol, "_make_session", return_value=session):
            resolved = paypal_protocol._follow_stripe_redirect(
                "https://pm-redirects.stripe.com/authorize/start",
                proxy="socks5h://127.0.0.1:7897",
                log=logs.append,
            )

        self.assertEqual(
            resolved,
            "https://www.paypal.com/agreements/approve?ba_token=BA-FOLLOW_123.456-789&x=1",
        )
        self.assertEqual(session.urls[-1], "https://pm-redirects.stripe.com/authorize/next")
        self.assertTrue(any("location=" in entry for entry in logs))
        self.assertTrue(any("body=" in entry for entry in logs))


if __name__ == "__main__":
    unittest.main()
