import unittest
from unittest.mock import patch

from sms_tool import payment_auth


class PaymentAuthTests(unittest.TestCase):
    def test_live_token_passes_without_relogin(self):
        account = {"email": "a@example.com", "access_token": "token-a"}
        with patch.object(payment_auth, "load_account_seed", return_value=(account, "session.json")), \
             patch("sms_tool.account_recovery.is_permanently_deactivated", return_value=False), \
             patch("sms_tool.account_liveness.probe_account_liveness", return_value={"ok": True, "status_code": 200}), \
             patch("sms_tool.account_recovery.relogin_codex_account") as relogin:
            result = payment_auth.ensure_payment_access_token(email="a@example.com")
        self.assertTrue(result["ok"])
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["access_token"], "token-a")
        relogin.assert_not_called()

    def test_401_oauth_refresh_reloads_persisted_token(self):
        before = {"email": "a@example.com", "access_token": "old"}
        after = {"email": "a@example.com", "access_token": "new"}
        with patch.object(payment_auth, "load_account_seed", side_effect=[(before, "session.json"), (after, "session.json")]), \
             patch("sms_tool.account_recovery.is_permanently_deactivated", return_value=False), \
             patch("sms_tool.account_liveness.probe_account_liveness", return_value={"ok": False, "status_code": 401}), \
             patch("sms_tool.account_recovery.relogin_codex_account", return_value={
                 "ok": True, "persisted": True, "probe": {"ok": True, "status_code": 200}
             }):
            result = payment_auth.ensure_payment_access_token(email="a@example.com")
        self.assertTrue(result["ok"])
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["token_changed"])
        self.assertEqual(result["access_token"], "new")

    def test_public_result_never_contains_credentials(self):
        public = payment_auth.public_payment_auth_result({
            "ok": True,
            "access_token": "secret",
            "auth_context": {"password": "secret"},
            "probe": {"status_code": 200},
        })
        self.assertEqual(public, {"ok": True, "probe": {"status_code": 200}})


if __name__ == "__main__":
    unittest.main()
