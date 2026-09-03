import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from sms_tool import mailbox_mailnest, mailbox_parsers
from sms_tool.mailbox_types import MailboxAccount


class FakeResponse:
    def __init__(self, body, status_code=200, text=""):
        self._body = body
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._body


def _cfg():
    return {
        "mailnest": {
            "enabled": True,
            "base_url": "https://mailnest.example",
            "api_key": "mn-secret",
            "project_code": "chatgpt001",
            "mode": "temporary",
            "timeout": 17,
        },
        "otp_poll_interval": 1,
    }


class MailNestParserTests(unittest.TestCase):
    def test_mailnest_graph_token_line_is_explicit_graph_provider(self):
        account = mailbox_parsers.parse_mailbox_pool_line(
            "mailnest://user@outlook.com----login-pass----9e5f94bc-e8a4-4e73-b8be-63364c29d753----rt-secret",
            "mailboxes.txt",
            1,
        )

        self.assertEqual(account.provider, "mailnest_graph")
        self.assertEqual(account.email, "user@outlook.com")
        self.assertEqual(account.password, "login-pass")
        self.assertEqual(account.token, "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
        self.assertEqual(account.refresh_token, "rt-secret")
        self.assertEqual(account.auth_mode, "oauth_refresh")

    def test_mailnest_api_line_keeps_receive_mode(self):
        account = mailbox_parsers.parse_mailbox_pool_line(
            "mailnest://user@outlook.com---user_mailbox---order-1",
            "mailboxes.txt",
            2,
        )

        self.assertEqual(account.provider, "mailnest")
        self.assertEqual(account.email, "user@outlook.com")
        self.assertEqual(account.auth_mode, "user_mailbox")
        self.assertEqual(account.token, "order-1")

    def test_mailbox_file_loader_accepts_mailnest_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text("mailnest://user@outlook.com---temporary---order-1\n", encoding="utf-8")

            accounts = mailbox_parsers._parse_mailbox_token_file(path)

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].provider, "mailnest")


class MailNestApiTests(unittest.TestCase):
    def test_temporary_purchase_uses_project_code_and_bearer_auth(self):
        response = FakeResponse({
            "code": "00000",
            "data": [{
                "id": "order-1",
                "email": "user@outlook.com",
                "project_code": "chatgpt001",
                "project_name": "ChatGPT",
                "price": "0.010",
            }],
        })
        args = Namespace(count=1, mailnest_mode="temporary", mailnest_project_code=None)
        with patch.object(mailbox_mailnest, "_email_cfg", return_value=_cfg()), \
             patch.object(mailbox_mailnest.curl_requests, "post", return_value=response) as post:
            accounts = mailbox_mailnest._create_mailnest_mailboxes(args)

        self.assertEqual(accounts[0].provider, "mailnest")
        self.assertEqual(accounts[0].auth_mode, "temporary")
        self.assertEqual(accounts[0].email, "user@outlook.com")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer mn-secret")
        self.assertEqual(kwargs["json"], {"count": 1, "project_code": "chatgpt001"})
        self.assertTrue(post.call_args.args[0].endswith("/api/v1/email/temporary/buy"))

    def test_user_mailbox_receive_normalizes_mailnest_message_to_graph_shape(self):
        response = FakeResponse({
            "code": "00000",
            "data": [{
                "id": "msg-1",
                "email": "user@outlook.com",
                "subject": "Your verification code",
                "from_email": "noreply@tm.openai.com",
                "to_email": "user@outlook.com",
                "body_preview": "Use this code.",
                "body": "<p>Use this code.</p>",
                "code_match": "123456",
                "received_at": "2026-06-24T10:14:51.992Z",
            }],
        })
        mailbox = MailboxAccount("user@outlook.com", provider="mailnest", auth_mode="user_mailbox")
        with patch.object(mailbox_mailnest.curl_requests, "post", return_value=response):
            messages = mailbox_mailnest._fetch_mailnest_messages(mailbox, email_cfg=_cfg())

        self.assertEqual(messages[0]["id"], "msg-1")
        self.assertEqual(messages[0]["from"]["emailAddress"]["address"], "noreply@tm.openai.com")
        self.assertIn("123456", messages[0]["bodyPreview"])
        self.assertEqual(messages[0]["toRecipients"][0]["emailAddress"]["address"], "user@outlook.com")

    def test_transient_receive_code_returns_empty_messages(self):
        response = FakeResponse({"code": "D0005", "msg": "try later", "data": None})
        mailbox = MailboxAccount("user@outlook.com", provider="mailnest", auth_mode="temporary")
        with patch.object(mailbox_mailnest.curl_requests, "post", return_value=response):
            messages = mailbox_mailnest._fetch_mailnest_messages(mailbox, email_cfg=_cfg())

        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
