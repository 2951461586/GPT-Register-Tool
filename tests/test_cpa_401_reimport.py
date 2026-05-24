import unittest
from unittest.mock import patch

from sms_tool import cpa_401_reimport
from sms_tool.mailbox import MailboxAccount


class Cpa401ReimportTests(unittest.TestCase):
    def test_has_deactivation_notice_matches_subject_body_and_email(self):
        mailbox = MailboxAccount(email="bad@example.com", refresh_token="rt", token="client", provider="chatai")
        messages = [
            {
                "subject": "Access deactivated",
                "bodyPreview": "We’re writing with an important update about your ChatGPT account associated with bad@example.com",
                "body": {"content": "Please contact support."},
                "receivedDateTime": "2026-05-24T00:00:00Z",
            }
        ]

        with patch.object(cpa_401_reimport, "_fetch_mailbox_messages", return_value=messages):
            result = cpa_401_reimport.has_deactivation_notice(mailbox, "bad@example.com")

        self.assertTrue(result["found"])
        self.assertEqual(result["subject"], "Access deactivated")

    def test_reimport_cpa_401_survivors_skips_deactivated_and_imports_survivor(self):
        bad = MailboxAccount(email="bad@example.com", refresh_token="rt1", token="client", provider="chatai")
        good = MailboxAccount(email="good@example.com", refresh_token="rt2", token="client", provider="chatai")
        auth_files = {
            "ok": True,
            "files": [
                {"email": "bad@example.com", "probe": {"status_code": 401}},
                {"email": "good@example.com", "probe": {"status_code": 401}},
                {"email": "active@example.com", "status": "active"},
            ],
        }

        def fake_notice(mailbox, email, limit=100, proxy=None):
            return {"found": email == "bad@example.com"}

        with patch.object(cpa_401_reimport, "fetch_target_auth_files", return_value=auth_files):
            with patch.object(cpa_401_reimport, "_load_mailbox_pool", return_value=[bad, good]):
                with patch.object(cpa_401_reimport, "has_deactivation_notice", side_effect=fake_notice):
                    with patch.object(cpa_401_reimport, "export_codex_session", return_value={"ok": True, "email": "good@example.com", "path": "codex-good.json"}) as exported:
                        with patch.object(cpa_401_reimport, "import_account_session", return_value={"ok": True, "email": "good@example.com"}) as imported:
                            result = cpa_401_reimport.reimport_cpa_401_survivors(chatai_mailbox_file="mailboxes.txt")

        self.assertTrue(result["ok"])
        self.assertEqual(result["total_401"], 2)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["skipped_deactivated"], 1)
        exported.assert_called_once()
        self.assertTrue(exported.call_args.kwargs["force_email_otp_login"])
        self.assertTrue(exported.call_args.kwargs["require_refresh_token"])
        imported.assert_called_once()

    def test_reimport_cpa_401_survivors_includes_cfworker_domain(self):
        auth_files = {
            "ok": True,
            "files": [
                {"email": "worker@edu.liziai.cloud", "probe": {"status_code": 401}},
            ],
        }

        with patch.object(cpa_401_reimport, "fetch_target_auth_files", return_value=auth_files):
            with patch.object(cpa_401_reimport, "_load_mailbox_pool", return_value=[]):
                with patch.object(cpa_401_reimport, "has_deactivation_notice", return_value={"found": False}) as checked:
                    with patch.object(cpa_401_reimport, "export_codex_session", return_value={"ok": True, "email": "worker@edu.liziai.cloud", "path": "codex-worker.json"}):
                        with patch.object(cpa_401_reimport, "import_account_session", return_value={"ok": True, "email": "worker@edu.liziai.cloud"}):
                            result = cpa_401_reimport.reimport_cpa_401_survivors(chatai_mailbox_file="mailboxes.txt")

        self.assertTrue(result["ok"])
        self.assertEqual(result["success"], 1)
        self.assertEqual(checked.call_args.args[0].provider, "cfworker")


if __name__ == "__main__":
    unittest.main()
