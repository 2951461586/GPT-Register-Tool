import unittest
from unittest.mock import patch
from types import SimpleNamespace

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

    def test_overview_marks_at_refreshed_when_oauth_succeeds_without_rt(self):
        overview = account_scan._scan_overview({
            "email": "a@example.com",
            "ok": True,
            "has_rt": False,
            "scan_status": "alive",
            "refresh": {"ok": False},
            "oauth": {"ok": True},
        })
        self.assertEqual(overview["at_status"], "AT失效已刷新")

    def test_overview_does_not_treat_negative_dropped_label_as_truthy(self):
        overview = account_scan._scan_overview({
            "email": "a@example.com",
            "scan_status": "phone_verification_required",
            "dropped": "否",
        })
        self.assertEqual(overview["dropped"], "否")

    def test_subscription_type_prefers_explicit_plan_type(self):
        self.assertEqual(account_scan._subscription_type({"plan_type": "plus"}), "plus")
        self.assertEqual(account_scan._subscription_type({"subscription_type": "team"}), "team")
        self.assertEqual(account_scan._subscription_type({"planType": "free"}), "free")


if __name__ == "__main__":
    unittest.main()
