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

    def test_reimport_cpa_401_survivors_uses_cpa_config_fallback_for_fetch(self):
        auth_files = {"ok": True, "files": []}

        with patch("sms_tool.import_targets._resolve_cpa_config", return_value=("https://cpa.example/v0/management/auth-files", "token")):
            with patch("sms_tool.import_targets.fetch_cpa_auth_files", return_value=auth_files) as fetched:
                result = cpa_401_reimport.reimport_cpa_401_survivors(chatai_mailbox_file="mailboxes.txt")

        self.assertTrue(result["ok"])
        fetched.assert_called_once_with("https://cpa.example/v0/management/auth-files", "token")

    def test_reimport_cpa_401_survivors_uses_quota_probe_for_active_cpa_file(self):
        good = MailboxAccount(email="good@example.com", refresh_token="rt2", token="client", provider="chatai")
        auth_files = {
            "ok": True,
            "files": [
                {"email": "good@example.com", "status": "active", "auth_index": "abc123", "type": "codex"},
            ],
        }

        with patch.object(cpa_401_reimport, "fetch_target_auth_files", return_value=auth_files):
            with patch.object(cpa_401_reimport, "probe_cpa_codex_quota", return_value={"ok": True, "status": "token_invalid", "status_code": 401}) as probed:
                with patch.object(cpa_401_reimport, "_load_mailbox_pool", return_value=[good]):
                    with patch.object(cpa_401_reimport, "has_deactivation_notice", return_value={"found": False}):
                        with patch.object(cpa_401_reimport, "export_codex_session", return_value={"ok": True, "email": "good@example.com", "path": "codex-good.json"}):
                            with patch.object(cpa_401_reimport, "import_account_session", return_value={"ok": True, "email": "good@example.com"}):
                                result = cpa_401_reimport.reimport_cpa_401_survivors(chatai_mailbox_file="mailboxes.txt")

        self.assertTrue(result["ok"])
        self.assertEqual(result["total_401"], 1)
        self.assertEqual(result["success"], 1)
        probed.assert_called_once()

    def test_reimport_cpa_401_survivors_skips_account_deactivated_export(self):
        dead = MailboxAccount(email="dead@example.com", refresh_token="rt", token="client", provider="chatai")
        auth_files = {
            "ok": True,
            "files": [
                {"email": "dead@example.com", "probe": {"status_code": 401}},
            ],
        }

        with patch.object(cpa_401_reimport, "fetch_target_auth_files", return_value=auth_files):
            with patch.object(cpa_401_reimport, "_load_mailbox_pool", return_value=[dead]):
                with patch.object(cpa_401_reimport, "has_deactivation_notice", return_value={"found": False}):
                    with patch.object(cpa_401_reimport, "export_codex_session", return_value={
                        "ok": False,
                        "email": "dead@example.com",
                        "error": "account_deactivated",
                        "terminal": True,
                    }):
                        with patch.object(cpa_401_reimport, "import_account_session") as imported:
                            result = cpa_401_reimport.reimport_cpa_401_survivors(chatai_mailbox_file="mailboxes.txt")

        self.assertTrue(result["ok"])
        self.assertEqual(result["total_401"], 1)
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["skipped_deactivated"], 1)
        self.assertEqual(result["results"][0]["reason"], "account_deactivated")
        imported.assert_not_called()


if __name__ == "__main__":
    unittest.main()
