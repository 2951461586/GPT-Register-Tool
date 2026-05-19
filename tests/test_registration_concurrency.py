import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool.mailbox import _parse_chatai_mailbox_file
from sms_tool.registration import _is_existing_login_redirect, run_batch


class RegistrationConcurrencyTests(unittest.TestCase):
    def test_prompt_login_query_is_not_existing_login_redirect(self):
        self.assertFalse(_is_existing_login_redirect(
            "https://chatgpt.com/api/auth/signin/openai?prompt=login&screen_hint=signup"
        ))
        self.assertFalse(_is_existing_login_redirect(
            "/api/accounts/authorize?prompt=login&screen_hint=signup"
        ))
        self.assertTrue(_is_existing_login_redirect("https://auth.openai.com/log-in"))

    def test_chatai_parser_repairs_misplaced_alias_plus(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "CierraRiste7566@+oai01hotmail.com----pw----client----refresh\n",
                encoding="utf-8",
            )

            records = _parse_chatai_mailbox_file(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].email, "cierrariste7566+oai01@hotmail.com")

    def test_run_batch_does_not_reuse_mailboxes_when_count_exceeds_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailboxes.txt"
            path.write_text(
                "a+oai01@hotmail.com----pw----client----refresh-a\n"
                "b+oai01@hotmail.com----pw----client----refresh-b\n",
                encoding="utf-8",
            )
            mailboxes = _parse_chatai_mailbox_file(path)

        seen = []

        def fake_run_email(**kwargs):
            mailbox = kwargs["mailbox"]
            seen.append(mailbox.email)
            return {"success": False, "email": mailbox.email, "error": "stub"}

        with patch("sms_tool.registration.run_email", side_effect=fake_run_email):
            results = run_batch(count=4, proxy=None, mailboxes=mailboxes, paypal_link=True, workers=4)

        self.assertEqual([r["email"] for r in results], ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])
        self.assertEqual(seen, ["a+oai01@hotmail.com", "b+oai01@hotmail.com"])


if __name__ == "__main__":
    unittest.main()
