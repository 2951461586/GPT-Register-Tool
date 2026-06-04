import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory

from sms_tool import cli


class OneClickSmsCliTests(unittest.TestCase):
    def test_one_click_sms_forces_one_phone_per_email(self):
        args = Namespace(max_reuse_count=5)

        self.assertEqual(cli._one_click_sms_max_reuse(args), 1)

    def test_view_inbox_loads_explicit_chatai_mailbox_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "chatai.txt"
            path.write_text("user@example.com----pw----client-id----refresh-token\n", encoding="utf-8")
            args = Namespace(
                email="user@example.com",
                chatai_mailbox_file=str(path),
                mailbox_file=None,
                email_refresh_token=None,
                email_access_token=None,
                email_password=None,
                luckmail_token=None,
                buy_luckmail_mailbox=False,
                buy_cfworker_mailbox=False,
            )

            mailbox = cli._mailbox_from_explicit_args(args)

        self.assertIsNotNone(mailbox)
        self.assertEqual(mailbox.email, "user@example.com")
        self.assertEqual(mailbox.token, "client-id")
        self.assertEqual(mailbox.refresh_token, "refresh-token")


if __name__ == "__main__":
    unittest.main()
