import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_one_click_sms_merges_explicit_mailbox_into_seed(self):
        class FakePhonePool:
            phones = [object()]
            total_capacity = 1

            def reset_exhausted_smsbower_slots(self):
                return None

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "chatai.txt"
            path.write_text("user@example.com----pw----client-id----refresh-token\n", encoding="utf-8")
            args = Namespace(
                email="user@example.com",
                email_file=None,
                session_file=None,
                chatai_mailbox_file=str(path),
                mailbox_file=None,
                max_reuse_count=0,
                phone_send_cooldown=None,
                phone_source="smsbower",
                workers=1,
                refresh_timeout=60,
                proxy="http://127.0.0.1:7897",
            )
            captured = {}

            def fake_refresh(data, **kwargs):
                captured.update(data)
                return {"ok": True, "refresh_token_status": "updated"}

            with (
                patch("sms_tool.phone_reuse.create_phone_pool", return_value=FakePhonePool()),
                patch("sms_tool.phone_reuse.print_phone_pool_status"),
                patch("sms_tool.session_refresh._load_seed_session", return_value=({"email": "user@example.com"}, "")),
                patch("sms_tool.codex_oauth.refresh_codex_oauth_session", side_effect=fake_refresh),
            ):
                cli._one_click_sms(args)

        self.assertEqual(captured["mailbox"]["email"], "user@example.com")
        self.assertEqual(captured["mailbox"]["provider"], "chatai")
        self.assertEqual(captured["mailbox"]["token"], "client-id")
        self.assertEqual(captured["mailbox"]["refresh_token"], "refresh-token")


if __name__ == "__main__":
    unittest.main()
