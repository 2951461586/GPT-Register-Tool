import unittest
from unittest.mock import patch

from sms_tool import account_scan


class AccountScanTests(unittest.TestCase):
    def test_detects_account_deactivated_typo_and_canonical(self):
        self.assertTrue(account_scan._looks_account_deactivated({"error": "account_deactivated"}))
        self.assertTrue(account_scan._looks_account_deactivated({"error": "account_deatived"}))
        self.assertTrue(account_scan._looks_account_deactivated({"body": "deleted or deactivated"}))

    def test_detects_phone_required(self):
        self.assertTrue(account_scan._looks_phone_required({"error": "add_phone_required"}))
        self.assertTrue(account_scan._looks_phone_required({"last_url": "https://auth.openai.com/add-phone"}))
        self.assertFalse(account_scan._looks_phone_required({"error": "passwordless_missing_mailbox"}))

    def test_no_rt_phone_required_does_not_persist_at_invalid(self):
        result = {
            "email": "a@example.com",
            "scan_status": "phone_verification_required",
            "phone_verification_required": True,
            "secondary_phone_verification_required": False,
        }
        with patch("sms_tool.account_scan.upsert_account") as upsert:
            account_scan._persist_scan(
                {
                    "email": "a@example.com",
                    "success": False,
                    "status": "at_invalid",
                    "error": "add_phone_required",
                    "paypal": {"status": "completed"},
                },
                "",
                result,
            )
        saved = upsert.call_args.args[0]
        self.assertTrue(saved["success"])
        self.assertEqual(saved["status"], "registered")
        self.assertNotIn("error", saved)


if __name__ == "__main__":
    unittest.main()
