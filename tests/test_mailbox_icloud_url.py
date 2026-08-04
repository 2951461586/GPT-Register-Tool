import tempfile
import unittest
import base64
from pathlib import Path
from unittest.mock import patch

from sms_tool import mailbox as mailbox_module
from sms_tool import mailbox_icloud_url, mailbox_parsers
from sms_tool.mail_otp import _email_otp_candidate
from sms_tool.mailbox_types import MailboxAccount


class _Response:
    def __init__(self, *, text="", payload=None, status_code=200):
        self.text = text
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class ICloudUrlMailboxTests(unittest.TestCase):
    def test_token_file_parses_three_and_four_hyphen_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icloud.txt"
            path.write_text(
                "first@icloud.com----https://icloud-api.example/show/secret/first@icloud.com\n"
                "second@icloud.com---http://mail.example/messages/secret/second@icloud.com\n",
                encoding="utf-8",
            )

            records = mailbox_parsers._parse_mailbox_token_file(path)

        self.assertEqual([record.email for record in records], ["first@icloud.com", "second@icloud.com"])
        self.assertTrue(all(record.provider == "icloud_url" for record in records))
        self.assertTrue(all(record.auth_mode == "otp_url" for record in records))

    def test_card_page_is_normalized_for_login_otp_filtering(self):
        page = """
        <head><meta charset="utf-8"><style>.outer{width:999999px}</style></head>
        <div class="card">
          <div class="fr">OpenAI &lt;noreply@openai.com&gt;</div>
          <div class="su">你的临时 ChatGPT 登录代码</div>
          <div class="dt">Mon, 03 Aug 2026 06:32:17 +0000 (UTC)</div>
          <div class="bd"><meta name="x"><style>.x{width:123456px}</style><div>你的临时代码：654321</div></div>
        </div>
        """
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://icloud-api.example/show/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(len(messages), 1)
        candidate = _email_otp_candidate(mailbox, messages[0], keyword="login code")
        self.assertEqual(candidate["otp"], "654321")
        self.assertNotIn("123456", messages[0]["body"]["content"])

    def test_yangyang_page_uses_list_and_detail_apis(self):
        page = """
        <script>
        var detailBase='/message/';
        var detailSuffix='/secret/target@icloud.com';
        var pageBase='/api/messages/secret/target@icloud.com';
        </script>
        """
        listing = {"items": [{
            "id": 42,
            "mailbox": "JUNK",
            "subject": "你的临时 ChatGPT 登录代码",
            "from_address": "OpenAI",
            "received_at": "2026-08-03 13:53:09",
        }], "has_more": False}
        encoded_body = base64.b64encode("<p>你的临时代码是 456789</p>".encode("utf-8")).decode("ascii")
        detail = {
            "body": "data:text/html;charset=utf-8;base64," + encoded_body,
            "fromAddress": "OpenAI",
            "html": False,
            "receivedAt": "2026-08-03 13:53:09",
            "subject": "你的临时 ChatGPT 登录代码",
        }
        responses = [_Response(text=page), _Response(payload=listing), _Response(payload=detail)]
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="http://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", side_effect=responses):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        candidate = _email_otp_candidate(mailbox, messages[0], keyword="login code")
        self.assertEqual(candidate["otp"], "456789")

    def test_mailbox_dispatch_and_credentials_use_otp_url_provider(self):
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/show/secret/target@icloud.com",
        )
        self.assertTrue(mailbox_module.mailbox_has_inbox_credentials(mailbox))
        with patch.object(mailbox_icloud_url, "fetch_icloud_url_messages", return_value=[{"id": "m1"}]) as fetch:
            messages = mailbox_module._fetch_mailbox_messages(mailbox, limit=1)
        self.assertEqual(messages, [{"id": "m1"}])
        fetch.assert_called_once()

    def test_poll_applies_icloud_timestamp_grace(self):
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/show/secret/target@icloud.com",
        )
        candidate = {"otp": "654321", "received_ts": 999}
        with (
            patch.object(mailbox_module, "_latest_email_otp_candidate", return_value=candidate) as latest,
            patch.object(mailbox_module, "_email_otp_settle_seconds", return_value=0),
            patch.object(mailbox_module, "_email_cfg", return_value={}),
        ):
            code = mailbox_module._poll_email_otp(
                mailbox,
                subject_keyword="login code",
                timeout=1,
                issued_after_unix=1000,
            )

        self.assertEqual(code, "654321")
        self.assertEqual(latest.call_args.kwargs["issued_after_unix"], 910)

    def test_request_error_does_not_expose_mailbox_url(self):
        secret_url = "https://mail.example/show/private-token/target@icloud.com"
        with patch.object(mailbox_icloud_url.curl_requests, "get", side_effect=RuntimeError(secret_url)):
            with self.assertRaisesRegex(RuntimeError, "iCloud OTP URL request failed: RuntimeError") as caught:
                mailbox_icloud_url._request(secret_url)
        self.assertNotIn("private-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
