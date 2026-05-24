import unittest
from unittest.mock import patch

from sms_tool import mailbox as mailbox_module
from sms_tool.mailbox import MailboxAccount
from sms_tool.providers import cfworker_mailbox
from sms_tool.providers.cfworker_mailbox import CFWorkerMailboxClient


class FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "code": 200,
            "data": {
                "page": 1,
                "pageSize": 20,
                "total": 2,
                "items": [
                    {
                        "message_id": "m1",
                        "from_address": "noreply@tm.openai.com",
                        "to_address": "target@edu.liziai.cloud",
                        "subject": "Your temporary ChatGPT verification code",
                        "extracted_json": '[{"value":"123456"}]',
                        "received_at": 1779588674891,
                    },
                    {
                        "message_id": "m2",
                        "from_address": "noreply@tm.openai.com",
                        "to_address": "other@edu.liziai.cloud",
                        "subject": "Your temporary ChatGPT verification code",
                        "extracted_json": '[{"value":"654321"}]',
                        "received_at": 1779588674891,
                    },
                ],
            },
        }


class CFWorkerMailboxClientTests(unittest.TestCase):
    def test_admin_email_list_uses_proxy_filters_recipient_and_exposes_otp_body(self):
        proxy = "socks5h://127.0.0.1:7897"
        client = CFWorkerMailboxClient(
            "https://worker.example",
            admin_token="admin",
            cf_api_token="cf",
            proxy=proxy,
        )

        with patch.object(cfworker_mailbox.curl_requests, "get", return_value=FakeResponse()) as get:
            messages = client.fetch_messages("target@edu.liziai.cloud", limit=5)

        self.assertEqual(get.call_args.kwargs["proxies"], {"http": proxy, "https": proxy})
        self.assertEqual(len(messages), 1)
        self.assertIn("123456", messages[0]["bodyPreview"])
        recipients = messages[0]["toRecipients"]
        self.assertEqual(recipients[0]["emailAddress"]["address"], "target@edu.liziai.cloud")
        self.assertEqual(messages[0]["receivedDateTime"], "2026-05-24T02:11:14Z")

    def test_cfworker_otp_poll_waits_for_newer_duplicate_code(self):
        mailbox = MailboxAccount(email="target@edu.liziai.cloud", provider="cfworker")
        old = {
            "id": "old",
            "receivedDateTime": "2026-05-24T02:47:01Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": '[{"value":"111111"}]',
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "target@edu.liziai.cloud"}}],
        }
        new = {
            "id": "new",
            "receivedDateTime": "2026-05-24T02:47:03Z",
            "subject": "Your temporary ChatGPT verification code",
            "bodyPreview": '[{"value":"222222"}]',
            "body": {"content": ""},
            "toRecipients": [{"emailAddress": {"address": "target@edu.liziai.cloud"}}],
        }

        with patch.object(mailbox_module, "_email_cfg", return_value={"cfworker_otp_settle_seconds": 0.01, "otp_poll_interval": 0.01}):
            with patch.object(mailbox_module, "_fetch_mailbox_messages", side_effect=[[old], [new], [new]]):
                code = mailbox_module._poll_email_otp(mailbox, timeout=1)

        self.assertEqual(code, "222222")


if __name__ == "__main__":
    unittest.main()
