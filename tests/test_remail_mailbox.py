import argparse
import io
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from sms_tool import mailbox as mailbox_router
from sms_tool import mailbox_remail
from sms_tool.mailbox_types import MailboxAccount


class FakeResponse:
    def __init__(self, body, status_code=200, text=""):
        self._body = body
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._body


def order_payload(index=1, mode="code"):
    return {
        "id": index,
        "orderNo": f"R{index}",
        "deliveryEmail": f"user{index}@outlook.com",
        "serviceToken": f"st-token-{index}",
        "serviceMode": mode,
        "payAmount": "0.80",
        "status": "active",
    }


class ReMailOrderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "enabled": True,
            "base_url": "https://remail.example",
            "api_key": "rk-secret-key",
            "project_id": 2,
            "product_id": 5,
            "service_mode": "code",
            "supply": "private_first",
            "email_suffix": "outlook.com",
        }

    def test_create_code_order_uses_bearer_idempotency_and_parses_account(self):
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.http_requests, "post", return_value=FakeResponse(order_payload())) as post:
            account = mailbox_remail._create_remail_order()

        self.assertEqual(account.provider, "remail")
        self.assertEqual(account.source, "remail_code")
        self.assertEqual(account.email, "user1@outlook.com")
        self.assertEqual(account.order_no, "R1")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer rk-secret-key")
        self.assertTrue(kwargs["headers"]["Idempotency-Key"])
        self.assertEqual(kwargs["params"], {"serviceMode": "code", "supply": "private_first"})
        self.assertEqual(kwargs["json"], {"projectId": 2, "productId": 5, "emailSuffix": "outlook.com"})

    def test_purchase_batch_returns_only_successful_orders(self):
        response = [
            {"index": 0, "status": "succeeded", "order": order_payload(1, "purchase")},
            {"index": 1, "status": "failed", "error": {"code": "insufficient_inventory", "message": "empty"}},
        ]
        args = argparse.Namespace(count=2)
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.http_requests, "post", return_value=FakeResponse(response)) as post, \
             redirect_stdout(io.StringIO()):
            accounts = mailbox_remail._create_remail_mailboxes(args, service_mode="purchase")

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].source, "remail_purchase")
        self.assertEqual(post.call_args.kwargs["json"]["quantity"], 2)
        self.assertEqual(post.call_args.kwargs["params"]["serviceMode"], "purchase")

    def test_http_error_redacts_api_key_and_service_token(self):
        token = "st-private-token"
        body = {"message": f"bad rk-secret-key {token}"}
        output = io.StringIO()
        with patch.object(mailbox_remail, "_remail_cfg", return_value=self.cfg), \
             patch.object(mailbox_remail.http_requests, "get", return_value=FakeResponse(body, 401)):
            with self.assertRaises(RuntimeError) as caught, redirect_stdout(output):
                mailbox_remail._remail_request("GET", "/v1/pickup", secrets=(token,))
        rendered = str(caught.exception) + output.getvalue()
        self.assertNotIn("rk-secret-key", rendered)
        self.assertNotIn(token, rendered)
        self.assertIn("[REDACTED]", rendered)


class ReMailPickupTests(unittest.TestCase):
    def setUp(self):
        self.account = MailboxAccount(
            email="alias@example.com",
            provider="remail",
            token="st-private-token",
            seen_message_id="10",
        )

    def test_pickup_normalizes_summary_and_fetches_detail_when_needed(self):
        summary = {
            "items": [
                {
                    "id": 11,
                    "sender": "noreply@tm.openai.com",
                    "recipient": "alias@example.com",
                    "receivedAt": "2026-07-21T08:00:00Z",
                    "subject": "Your login code",
                    "bodyPreview": "Open the message to continue",
                    "verificationCode": "",
                }
            ]
        }
        detail = dict(summary["items"][0], body="Your verification code is 654321")
        responses = [FakeResponse(summary), FakeResponse(detail)]
        with patch.object(mailbox_remail.http_requests, "get", side_effect=responses) as get:
            messages = mailbox_remail._fetch_remail_messages(self.account, proxy="http://127.0.0.1:7897")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "11")
        self.assertEqual(messages[0]["from"], "noreply@tm.openai.com")
        self.assertEqual(messages[0]["toRecipients"][0]["emailAddress"]["address"], "alias@example.com")
        self.assertIn("654321", messages[0]["body"]["content"])
        self.assertEqual(get.call_count, 2)
        self.assertNotIn("Authorization", get.call_args_list[0].kwargs["headers"])
        self.assertEqual(get.call_args_list[0].kwargs["params"]["token"], "st-private-token")
        self.assertEqual(get.call_args_list[0].kwargs["proxies"]["https"], "http://127.0.0.1:7897")

    def test_candidate_filters_snapshot_time_recipient_and_excluded_code(self):
        issued_after = int(time.time()) - 10

        def message(message_id, code, recipient="alias@example.com", offset=0):
            received = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(issued_after + offset))
            return mailbox_remail._normalize_remail_message({
                "id": message_id,
                "sender": "noreply@tm.openai.com",
                "recipient": recipient,
                "receivedAt": received,
                "subject": "Your verification code",
                "bodyPreview": f"Your verification code is {code}",
                "verificationCode": code,
            })

        messages = [
            message(10, "111111", offset=5),
            message(11, "222222", offset=-5),
            message(12, "333333", recipient="other@example.com", offset=6),
            message(13, "444444", offset=7),
            message(14, "555555", offset=8),
        ]
        candidate = mailbox_remail._latest_remail_otp_candidate(
            self.account,
            messages,
            issued_after_unix=issued_after,
            excluded_otps={"555555"},
        )
        self.assertEqual(candidate["otp"], "444444")
        self.assertEqual(candidate["id"], "13")


class ReMailRouterTests(unittest.TestCase):
    def test_load_purchase_pool_and_default_auto_create_route_to_remail(self):
        expected = MailboxAccount(email="user@example.com", provider="remail", token="st")
        args = argparse.Namespace(buy_remail_mailbox=True, buy_cfworker_mailbox=False, remail_service_mode=None)
        with patch.object(mailbox_router.mailbox_remail, "_create_remail_mailboxes", return_value=[expected]) as create:
            self.assertEqual(mailbox_router._load_mailbox_pool(args), [expected])
            create.assert_called_once_with(args, service_mode="purchase")

        with patch.object(mailbox_router, "_remail_enabled", return_value=True), \
             patch.object(mailbox_router.mailbox_remail, "_create_remail_order", return_value=expected) as create:
            self.assertIs(mailbox_router._ensure_mailbox_account(), expected)
            create.assert_called_once_with(service_mode="code")

    def test_explicit_code_mode_creates_remail_pool_for_desktop_one_click(self):
        expected = MailboxAccount(email="user@example.com", provider="remail", token="st")
        args = argparse.Namespace(
            buy_remail_mailbox=False,
            buy_cfworker_mailbox=False,
            remail_service_mode="code",
        )
        with patch.object(mailbox_router.mailbox_remail, "_create_remail_mailboxes", return_value=[expected]) as create:
            self.assertEqual(mailbox_router._load_mailbox_pool(args), [expected])
            create.assert_called_once_with(args, service_mode="code")

    def test_direct_service_token_requires_email_and_uses_remail_provider(self):
        cfg = {"service_token": "st-config", "delivery_email": "saved@example.com", "order_no": "R9"}
        with patch.object(mailbox_router.mailbox_remail, "_remail_cfg", return_value=cfg), \
             patch.object(mailbox_router, "_gmail_mailbox_from_config", return_value=None):
            account = mailbox_router._mailbox_from_config(argparse.Namespace(remail_token=None, email=None))
        self.assertEqual(account.provider, "remail")
        self.assertEqual(account.email, "saved@example.com")
        self.assertEqual(account.token, "st-config")


if __name__ == "__main__":
    unittest.main()
