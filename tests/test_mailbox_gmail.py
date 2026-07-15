import unittest
from email.header import Header
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sms_tool import mailbox as mailbox_module, mailbox_gmail, mailbox_parsers
from sms_tool.mailbox_types import MailboxAccount


class _FakeImap:
    def __init__(self, *_args, **_kwargs):
        self.logged_in = False
        self.selected = None
        self.message = EmailMessage()
        self.message["Subject"] = Header("你的临时 ChatGPT 登录代码", "utf-8").encode()
        self.message["From"] = str(Header("OpenAI 团队", "utf-8")) + " <noreply@tm.openai.com>"
        self.message["To"] = "target@gmail.com"
        self.message["Date"] = "Sun, 05 Jul 2026 10:00:00 +0800"
        self.message["Message-ID"] = "<gmail-test-message@example.com>"
        self.message.set_content("Verification code: 654321")

    def login(self, email, password):
        self.logged_in = (email, password)
        return "OK", [b"logged-in"]

    def authenticate(self, mechanism, callback):
        _ = callback(None)
        self.logged_in = mechanism
        return "OK", [b"authenticated"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "[Gmail]/Spam"']

    def select(self, folder, readonly=True):
        self.selected = (folder, readonly)
        return "OK", [b"1"]

    def search(self, *_args):
        return "OK", [b"1"]

    def fetch(self, *_args):
        return "OK", [(b"1 (RFC822 {123})", self.message.as_bytes())]

    def logout(self):
        return "BYE", [b"logout"]


class _FakeSmtp:
    def __init__(self, *_args, **_kwargs):
        self.logged_in = None
        self.sent = None

    def login(self, email, password):
        self.logged_in = (email, password)

    def send_message(self, message):
        self.sent = message

    def quit(self):
        return 221, b"bye"


class GmailMailboxTests(unittest.TestCase):
    def test_parse_gmail_app_password_line(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "gmail.txt"
            path.write_text("gmail://user@gmail.com---abcd efgh ijkl mnop\n", encoding="utf-8")

            records = mailbox_parsers._parse_mailbox_token_file(path)

        self.assertEqual(len(records), 1)
        mailbox = records[0]
        self.assertEqual(mailbox.provider, "gmail")
        self.assertEqual(mailbox.auth_mode, "app_password")
        self.assertEqual(mailbox.password, "abcd efgh ijkl mnop")

    def test_parse_gmail_oauth_line(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "gmail.txt"
            path.write_text(
                "gmail://user@gmail.com----client-id.apps.googleusercontent.com----client-secret----refresh-token----access-token\n",
                encoding="utf-8",
            )

            records = mailbox_parsers._parse_mailbox_token_file(path)

        self.assertEqual(len(records), 1)
        mailbox = records[0]
        self.assertEqual(mailbox.provider, "gmail")
        self.assertEqual(mailbox.auth_mode, "oauth_refresh")
        self.assertEqual(mailbox.token, "client-id.apps.googleusercontent.com")
        self.assertEqual(mailbox.client_secret, "client-secret")
        self.assertEqual(mailbox.refresh_token, "refresh-token")

    def test_fetch_gmail_imap_messages_with_app_password(self):
        mailbox = MailboxAccount(email="target@gmail.com", provider="gmail", password="abcd efgh ijkl mnop", auth_mode="app_password")

        with patch("sms_tool.mailbox_gmail.imaplib.IMAP4_SSL", _FakeImap):
            messages = mailbox_gmail.fetch_gmail_imap_messages(mailbox, limit=5)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["_source"], "outlook_imap")
        self.assertEqual(messages[0]["subject"], "你的临时 ChatGPT 登录代码")
        self.assertIn("noreply@tm.openai.com", messages[0]["from"])
        self.assertIn("654321", messages[0]["body"]["content"])

    def test_fetch_gmail_imap_messages_uses_proxy_client(self):
        mailbox = MailboxAccount(email="target@gmail.com", provider="gmail", password="abcd efgh ijkl mnop", auth_mode="app_password")

        with patch("sms_tool.mailbox_gmail._imap_ssl_client", return_value=_FakeImap()) as client:
            messages = mailbox_gmail.fetch_gmail_imap_messages(
                mailbox,
                limit=5,
                proxy="socks5h://127.0.0.1:7897",
                timeout=12,
            )

        self.assertEqual(len(messages), 1)
        client.assert_called_once_with(
            "imap.gmail.com",
            993,
            proxy="socks5h://127.0.0.1:7897",
            timeout=12,
        )

    def test_socks5h_connect_request_uses_remote_dns(self):
        request = mailbox_gmail._socks5_connect_request("imap.gmail.com", 993, remote_dns=True)

        self.assertEqual(request[:5], b"\x05\x01\x00\x03\x0e")
        self.assertIn(b"imap.gmail.com", request)
        self.assertEqual(request[-2:], (993).to_bytes(2, "big"))

    def test_fetch_mailbox_messages_passes_resolved_proxy_to_gmail_imap(self):
        mailbox = MailboxAccount(email="target@gmail.com", provider="gmail", password="abcd efgh ijkl mnop", auth_mode="app_password")

        with patch.object(mailbox_module, "CFG", {"mailbox_proxy": "socks5h://127.0.0.1:7897"}), \
             patch.object(mailbox_module, "_email_cfg", return_value={"gmail": {"imap_enabled": True}}), \
             patch.object(mailbox_module.mailbox_gmail, "fetch_gmail_imap_messages", return_value=[]) as fetch:
            messages = mailbox_module._fetch_mailbox_messages(
                mailbox,
                limit=5,
                proxy="socks5h://127.0.0.1:1080",
            )

        self.assertEqual(messages, [])
        self.assertEqual(fetch.call_args.kwargs["proxy"], "socks5h://127.0.0.1:7897")

    def test_gmail_config_requires_exact_login_email(self):
        with patch.object(mailbox_module, "_email_cfg", return_value={
            "gmail": {
                "enabled": True,
                "email": "liziaicloudxm@gmail.com",
                "app_password": "abcd efgh ijkl mnop",
                "auth_mode": "app_password",
            }
        }):
            mailbox = mailbox_module._gmail_mailbox_from_config(
                type("Args", (), {"email": "liziaiclou.dxm+pj8@gmail.com"})()
            )

        self.assertIsNone(mailbox)

    def test_gmail_alias_does_not_reuse_token_file_credentials(self):
        with TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "mailbox_tokens.txt"
            token_file.write_text(
                "gmail://migueladorno236@gmail.com---qrst uvwx yzab vfgv\n",
                encoding="utf-8",
            )
            with patch.object(mailbox_module, "_email_cfg", return_value={
                "token_file": str(token_file),
                "gmail": {
                    "enabled": True,
                    "email": "liziaicloudxm@gmail.com",
                    "app_password": "abcd efgh ijkl tnht",
                    "auth_mode": "app_password",
                },
            }):
                mailbox = mailbox_module._gmail_mailbox_from_config(
                    type("Args", (), {"email": "mi.g.u.el.ad.o.rno236+43wqm@gmail.com"})()
                )

        self.assertIsNone(mailbox)

    def test_send_gmail_message_with_app_password(self):
        mailbox = MailboxAccount(
            email="sender@gmail.com",
            provider="gmail",
            password="abcd efgh ijkl mnop",
            auth_mode="app_password",
            sender_name="Sender",
        )
        fake = _FakeSmtp()

        with patch("sms_tool.mailbox_gmail.smtplib.SMTP_SSL", return_value=fake):
            result = mailbox_gmail.send_gmail_message(
                mailbox,
                "receiver@example.com",
                subject="Test",
                text_body="Hello",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(fake.logged_in, ("sender@gmail.com", "abcdefghijklmnop"))
        self.assertEqual(fake.sent["To"], "receiver@example.com")
        self.assertEqual(fake.sent["Subject"], "Test")


if __name__ == "__main__":
    unittest.main()
