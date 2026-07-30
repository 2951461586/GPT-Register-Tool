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

    def test_overview_marks_at_refreshed_when_oauth_succeeds_without_rt(self):
        overview = account_scan._scan_overview(
            {
                "email": "a@example.com",
                "ok": True,
                "has_rt": False,
                "scan_status": "alive",
                "refresh": {"ok": False},
                "oauth": {"ok": True},
            }
        )
        self.assertEqual(overview["at_status"], "AT失效已刷新")

    def test_overview_does_not_treat_negative_dropped_label_as_truthy(self):
        overview = account_scan._scan_overview(
            {
                "email": "a@example.com",
                "scan_status": "phone_verification_required",
                "dropped": "否",
            }
        )
        self.assertEqual(overview["dropped"], "否")

    def test_subscription_type_prefers_explicit_plan_type(self):
        self.assertEqual(account_scan._subscription_type({"plan_type": "plus"}), "plus")
        self.assertEqual(account_scan._subscription_type({"subscription_type": "team"}), "team")
        self.assertEqual(account_scan._subscription_type({"planType": "free"}), "free")

    def test_workspace_probe_transport_error_is_inconclusive(self):
        with patch(
            "sms_tool.account_scan.inspect_workspace",
            side_effect=RuntimeError("Failed to perform, curl: (56) Recv failure: Connection was reset"),
        ):
            result = account_scan._workspace_probe(
                {"email": "a@example.com", "cookie_header": "s=1"},
                enabled=True,
            )
        self.assertEqual(result["status"], "workspace_check_inconclusive")
        self.assertIn("curl: (56)", result["error"])

    def test_scan_one_disables_workspace_by_default(self):
        with patch("sms_tool.account_scan._load_seed_session", return_value=({"email": "a@example.com"}, "")), \
             patch("sms_tool.account_scan._workspace_probe", return_value={"ok": True, "status": "workspace_check_disabled"}) as workspace_probe, \
             patch("sms_tool.account_scan._openai_refresh_token", return_value=""), \
             patch("sms_tool.account_scan.collect_codex_oauth_tokens", return_value={"ok": True, "tokens": {"access_token": "at_123"}}), \
             patch("sms_tool.account_scan._persist_scan"):
            result = account_scan._scan_one(0, 1, "a@example.com")

        self.assertTrue(result["ok"])
        self.assertFalse(workspace_probe.call_args.kwargs["enabled"])

    def test_gmail_scan_lane_key_keeps_exact_address(self):
        self.assertEqual(
            account_scan._gmail_scan_lane_key("M.i.g.u.EL.A.D.orno236+qrzzsw@gmail.com"),
            "m.i.g.u.el.a.d.orno236+qrzzsw@gmail.com",
        )

    def test_persist_scan_clears_workspace_id_for_free_account(self):
        result = {
            "email": "a@example.com",
            "scan_status": "alive",
            "workspace": {
                "status": "workspace_ok",
                "actual_workspace_id": "ws-1",
                "actual_workspace_name": "Hidden",
                "account_type_after": "free",
            },
        }
        with patch("sms_tool.account_scan.upsert_account") as upsert:
            account_scan._persist_scan({"email": "a@example.com"}, "", result)
        saved = upsert.call_args.args[0]
        self.assertEqual(saved["workspace_id"], "")
        self.assertEqual(saved["workspace_name"], "")
        self.assertEqual(saved["account_type"], "free")

    def test_scan_one_keeps_alive_when_workspace_check_is_inconclusive(self):
        with patch("sms_tool.account_scan._load_seed_session", return_value=({"email": "a@example.com"}, "")), \
             patch("sms_tool.account_scan._workspace_probe", return_value={"ok": False, "status": "workspace_check_inconclusive", "error": "curl: (56)"}), \
             patch("sms_tool.account_scan._openai_refresh_token", return_value=""), \
             patch("sms_tool.account_scan.collect_codex_oauth_tokens", return_value={"ok": True, "tokens": {"access_token": "at_123"}}), \
             patch("sms_tool.account_scan._persist_scan"):
            result = account_scan._scan_one(0, 1, "a@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["scan_status"], "alive")
        self.assertEqual(result["workspace"]["status"], "workspace_check_inconclusive")

    def test_scan_one_relogin_recovers_token_invalid_before_scan_failure(self):
        with patch(
            "sms_tool.account_scan._load_seed_session",
            side_effect=[
                ({"email": "a@example.com", "access_token": "old_at"}, "session.json"),
                ({"email": "a@example.com", "access_token": "new_at", "oauth_refresh_token": "new_rt"}, "session.json"),
            ],
        ), \
             patch("sms_tool.account_scan.probe_local_codex_quota", side_effect=[
                 {"ok": False, "status": "token_invalid", "quota_status": "401失效"},
                 {"ok": True, "status": "active", "quota_status": "3/5"},
             ]), \
             patch("sms_tool.account_scan.relogin_codex_account", return_value={"ok": True, "mode": "codex_oauth_pkce", "probe": {"ok": True, "status": "active", "status_code": 200, "quota_status": "3/5"}}) as relogin, \
             patch("sms_tool.account_scan._workspace_probe", return_value={"ok": True, "status": "workspace_check_disabled"}), \
             patch("sms_tool.account_scan._openai_refresh_token", return_value=""), \
             patch("sms_tool.account_scan.collect_codex_oauth_tokens") as collect, \
             patch("sms_tool.account_scan._persist_scan"):
            result = account_scan._scan_one(0, 1, "a@example.com", quota_relogin_on_401=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["scan_status"], "alive")
        self.assertTrue(result["relogin"]["ok"])
        self.assertEqual(result["token_probe"]["status"], "active")
        relogin.assert_called_once()
        self.assertEqual(relogin.call_args.kwargs["mode"], "codex_oauth")
        collect.assert_not_called()

    def test_scan_one_keeps_alive_when_existing_at_is_valid_but_auth_probe_transport_fails(self):
        with patch("sms_tool.account_scan._load_seed_session", return_value=({"email": "a@example.com", "access_token": "at_123"}, "")), \
             patch("sms_tool.account_scan.probe_local_codex_quota", return_value={"ok": True, "status": "active", "quota_status": "3/5"}), \
             patch("sms_tool.account_scan._workspace_probe", return_value={"ok": False, "status": "workspace_check_inconclusive", "error": "curl: (56)"}), \
             patch("sms_tool.account_scan._openai_refresh_token", return_value=""), \
             patch(
                 "sms_tool.account_scan.collect_codex_oauth_tokens",
                 return_value={"ok": False, "error": "Failed to perform, curl: (56) CONNECT tunnel failed, response 403"},
             ), \
             patch("sms_tool.account_scan._persist_scan"):
            result = account_scan._scan_one(0, 1, "a@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(result["scan_status"], "alive_probe_inconclusive")
        self.assertEqual(result["token_probe"]["status"], "active")

    def test_scan_one_marks_relogin_failed_instead_of_generic_scan_failed(self):
        with patch(
            "sms_tool.account_scan._load_seed_session",
            return_value=({"email": "a@example.com", "access_token": "old_at"}, "session.json"),
        ), \
             patch("sms_tool.account_scan.probe_local_codex_quota", return_value={"ok": False, "status": "token_invalid", "quota_status": "401失效"}), \
             patch(
                "sms_tool.account_scan.relogin_codex_account",
                return_value={"ok": False, "error": "passwordless_missing_mailbox"},
            ), \
             patch("sms_tool.account_scan._workspace_probe", return_value={"ok": True, "status": "workspace_check_disabled"}), \
             patch("sms_tool.account_scan._openai_refresh_token", return_value=""), \
             patch("sms_tool.account_scan.collect_codex_oauth_tokens") as collect, \
             patch("sms_tool.account_scan._persist_scan"):
            result = account_scan._scan_one(0, 1, "a@example.com", quota_relogin_on_401=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["scan_status"], "relogin_failed")
        self.assertEqual(result["relogin"]["error"], "passwordless_missing_mailbox")
        collect.assert_not_called()

    def test_scan_one_passes_codex_only_relogin_mode(self):
        with patch(
            "sms_tool.account_scan._load_seed_session",
            side_effect=[
                ({"email": "a@example.com", "access_token": "old_at"}, "session.json"),
                ({"email": "a@example.com", "access_token": "new_at"}, "session.json"),
            ],
        ), \
             patch("sms_tool.account_scan.probe_local_codex_quota", side_effect=[
                 {"ok": False, "status": "token_invalid", "quota_status": "401失效"},
                 {"ok": True, "status": "active", "quota_status": "1/5"},
             ]), \
             patch("sms_tool.account_scan.relogin_codex_account", return_value={"ok": True, "mode": "codex_oauth_pkce"}) as relogin, \
             patch("sms_tool.account_scan._workspace_probe", return_value={"ok": True, "status": "workspace_check_disabled"}), \
             patch("sms_tool.account_scan._openai_refresh_token", return_value=""), \
             patch("sms_tool.account_scan.collect_codex_oauth_tokens") as collect, \
             patch("sms_tool.account_scan._persist_scan"):
            result = account_scan._scan_one(
                0,
                1,
                "a@example.com",
                quota_relogin_on_401=True,
                relogin_mode="codex_oauth",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(relogin.call_args.kwargs["mode"], "codex_oauth")
        collect.assert_not_called()

    def test_scan_one_skips_all_network_relogin_for_terminal_account(self):
        data = {"email": "a@example.com", "status": "account_deactivated", "access_token": "old_at"}
        with patch("sms_tool.account_scan._load_seed_session", return_value=(data, "session.json")), \
             patch("sms_tool.account_scan.probe_local_codex_quota") as probe, \
             patch("sms_tool.account_scan.relogin_codex_account") as relogin, \
             patch("sms_tool.account_scan.collect_codex_oauth_tokens") as collect, \
             patch("sms_tool.account_scan._persist_scan"):
            result = account_scan._scan_one(0, 1, "a@example.com", quota_relogin_on_401=True)

        self.assertEqual(result["scan_status"], "account_deactivated")
        self.assertTrue(result["terminal"])
        probe.assert_not_called()
        relogin.assert_not_called()
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
