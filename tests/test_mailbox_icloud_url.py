import tempfile
import unittest
import base64
from argparse import Namespace
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

    def test_real_world_shape_works_through_legacy_mixed_file_route(self):
        line = (
            "target@icloud.com----"
            "https://mail.example/messages/AbCd_0123-credential/target%40icloud.com"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icloud.txt"
            path.write_text("\ufeff" + line + "\r\n", encoding="utf-8")

            records = mailbox_parsers._parse_chatai_mailbox_file(path)
            loaded = mailbox_module._load_mailbox_pool(Namespace(chatai_mailbox_file=str(path)))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].provider, "icloud_url")
        self.assertEqual(records[0].auth_mode, "otp_url")
        self.assertEqual([record.email for record in loaded], ["target@icloud.com"])
        self.assertEqual(loaded[0].token, records[0].token)

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

    def test_card_page_tolerates_unescaped_sender_address(self):
        page = """
        <div class="card">
          <div class="fr">OpenAI <noreply_at_tm_openai_com@icloud.com></div>
          <div class="su">Your temporary ChatGPT verification code</div>
          <div class="dt">Wed, 05 Aug 2026 08:03:00 +0000</div>
          <div class="bd"><html><body>Your login code is 654321</body></html></div>
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

    def test_mail_card_article_page_is_normalized_for_otp_polling(self):
        page = """
        <article class="mail-card">
          <details open>
            <summary>
              <span class="subject">Your temporary ChatGPT verification code</span>
              <span class="date">2026-08-04 14:12:35</span>
            </summary>
            <div class="meta">Sender: OpenAI &lt;noreply@openai.com&gt;</div>
            <pre class="body">Enter this temporary verification code to continue: 654321</pre>
          </details>
        </article>
        """
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=10)

        self.assertEqual(len(messages), 1)
        candidate = _email_otp_candidate(mailbox, messages[0], keyword="login code|verification code")
        self.assertEqual(candidate["otp"], "654321")
        self.assertIn("noreply@openai.com", messages[0]["from"])

    def test_card_messages_are_normalized_to_newest_first(self):
        page = """
        <div class="card">
          <div class="fr">OpenAI &lt;noreply@openai.com&gt;</div>
          <div class="su">Your ChatGPT login code</div>
          <div class="dt">Mon, 03 Aug 2026 06:31:00 +0000</div>
          <div class="bd">Your code is 111111</div>
        </div>
        <div class="card">
          <div class="fr">OpenAI &lt;noreply@openai.com&gt;</div>
          <div class="su">Your ChatGPT login code</div>
          <div class="dt">Mon, 03 Aug 2026 06:32:00 +0000</div>
          <div class="bd">Your code is 222222</div>
        </div>
        """
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(mailbox_icloud_url.curl_requests, "get", return_value=_Response(text=page)):
            messages = mailbox_icloud_url.fetch_icloud_url_messages(mailbox, limit=1)

        self.assertEqual(
            [_email_otp_candidate(mailbox, message, keyword="login code")["otp"] for message in messages],
            ["222222"],
        )

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

    def test_snapshot_uses_yangyang_listing_without_fetching_message_details(self):
        page = """
        <script>
        var detailBase='/message/';
        var detailSuffix='/secret/target@icloud.com';
        var pageBase='/api/messages/secret/target@icloud.com';
        </script>
        """
        listing = {"items": [
            {"id": 41, "subject": "older", "received_at": "2026-08-03 13:52:09"},
            {"id": 42, "subject": "newer", "received_at": "2026-08-03 13:53:09"},
        ]}
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="http://mail.example/messages/secret/target@icloud.com",
        )
        with patch.object(
            mailbox_icloud_url.curl_requests,
            "get",
            side_effect=[_Response(text=page), _Response(payload=listing)],
        ) as get:
            message_id = mailbox_module._snapshot_mailbox_message(mailbox)

        self.assertEqual(message_id, "42")
        self.assertEqual(mailbox.seen_message_ids, ("42", "41"))
        self.assertGreater(mailbox.seen_message_received_ts, 0)
        self.assertEqual(get.call_count, 2)

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

    def test_snapshot_message_and_older_messages_are_not_reused(self):
        mailbox = MailboxAccount(
            email="target@icloud.com",
            provider="icloud_url",
            token="https://mail.example/messages/secret/target@icloud.com",
            seen_message_id="snapshot",
        )
        old_messages = [
            self._otp_message("snapshot", "111111", "2026-08-04T10:00:00+00:00"),
            self._otp_message("older", "222222", "2026-08-04T09:59:30+00:00"),
            self._otp_message("older-undated", "555555", ""),
        ]
        mailbox.seen_message_ids = tuple(message["id"] for message in old_messages)
        mailbox.seen_message_received_ts = mailbox_module._message_received_ts(old_messages[0])
        self.assertIsNone(mailbox_module._latest_email_otp_candidate(
            mailbox,
            keyword="login code",
            issued_after_unix=0,
            override_messages=old_messages,
        ))

        new_messages = [
            self._otp_message("new", "333333", "2026-08-04T10:00:10+00:00"),
            *old_messages,
        ]
        candidate = mailbox_module._latest_email_otp_candidate(
            mailbox,
            keyword="login code",
            issued_after_unix=0,
            override_messages=new_messages,
        )
        self.assertEqual(candidate["otp"], "333333")
        self.assertEqual(candidate["id"], "new")

        undated = self._otp_message("new-undated", "444444", "")
        candidate = mailbox_module._latest_email_otp_candidate(
            mailbox,
            keyword="login code",
            issued_after_unix=0,
            override_messages=[*old_messages, undated],
        )
        self.assertEqual(candidate["otp"], "444444")
        self.assertEqual(candidate["id"], "new-undated")

    @staticmethod
    def _otp_message(message_id, code, received_at):
        return {
            "id": message_id,
            "subject": "Your ChatGPT login code",
            "from": "OpenAI <noreply@openai.com>",
            "receivedDateTime": received_at,
            "body": {"content": f"Your verification code is {code}"},
            "toRecipients": [{"emailAddress": {"address": "target@icloud.com"}}],
        }

    def test_request_error_does_not_expose_mailbox_url(self):
        secret_url = "https://mail.example/show/private-token/target@icloud.com"
        with patch.object(mailbox_icloud_url.curl_requests, "get", side_effect=RuntimeError(secret_url)):
            with self.assertRaisesRegex(RuntimeError, "iCloud OTP URL request failed: RuntimeError") as caught:
                mailbox_icloud_url._request(secret_url)
        self.assertNotIn("private-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
