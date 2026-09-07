import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli


class CliQuotaTests(unittest.TestCase):
    @staticmethod
    def _args(**overrides):
        values = {
            "email": "user@example.com",
            "email_file": None,
            "refresh_local_quota": False,
            "quota_mode": "auto",
            "quota_workers": 2,
            "workers": 2,
            "proxy": None,
            "refresh_timeout": 30,
            "quota_auto_relogin": True,
            "quota_relogin_timeout": 300,
            "scan_relogin_mode": "auto",
            "cpa_api_url": None,
            "cpa_api_token": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_refresh_local_quota_enables_requested_401_recovery_chain(self):
        args = SimpleNamespace(
            email="user@example.com",
            email_file=None,
            refresh_local_quota=True,
            quota_mode="local",
            quota_workers=2,
            workers=2,
            proxy=None,
            refresh_timeout=30,
            quota_auto_relogin=True,
            quota_relogin_timeout=300,
            scan_relogin_mode="auto",
            cpa_api_url=None,
            cpa_api_token=None,
        )
        result = {"ok": True, "total": 1, "success": 1, "failed": 0, "results": []}
        with patch("sms_tool.accounts.account_recovery.refresh_local_quota_statuses", return_value=result) as refresh:
            with redirect_stdout(io.StringIO()):
                cli._refresh_cpa_quota(args)

        self.assertTrue(refresh.call_args.kwargs["relogin_on_401"])
        self.assertEqual(refresh.call_args.kwargs["relogin_timeout"], 300)
        self.assertEqual(refresh.call_args.kwargs["relogin_mode"], "auto")

    def test_relogin_timeout_default_has_margin_over_otp_window(self):
        """The default must clear the 180s OTP window (config.json email.otp_timeout)
        with headroom; a 180s default left zero margin and every poll timed out."""
        args = SimpleNamespace(
            email="user@example.com",
            email_file=None,
            refresh_local_quota=True,
            quota_mode="local",
            quota_workers=2,
            workers=2,
            proxy=None,
            refresh_timeout=30,
            quota_auto_relogin=True,
            scan_relogin_mode="auto",
            cpa_api_url=None,
            cpa_api_token=None,
            # quota_relogin_timeout intentionally absent -> exercise the default.
        )
        result = {"ok": True, "total": 1, "success": 1, "failed": 0, "results": []}
        with patch("sms_tool.accounts.account_recovery.refresh_local_quota_statuses", return_value=result) as refresh:
            with redirect_stdout(io.StringIO()):
                cli._refresh_cpa_quota(args)

        self.assertEqual(refresh.call_args.kwargs["relogin_timeout"], 300)

    def test_auto_quota_does_not_fallback_terminal_deactivation(self):
        args = self._args()
        local = {
            "ok": False,
            "total": 1,
            "success": 0,
            "failed": 1,
            "results": [{
                "email": "user@example.com",
                "ok": False,
                "probe": {"ok": False, "status": "account_deactivated", "terminal": True},
                "persisted": True,
            }],
        }
        with (
            patch("sms_tool.accounts.account_recovery.refresh_local_quota_statuses", return_value=local),
            patch("sms_tool.cpa_import.refresh_cpa_quota_statuses") as fallback,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli._refresh_cpa_quota(args)

        self.assertEqual(raised.exception.code, 3)
        fallback.assert_not_called()

    def test_auto_quota_requires_complete_cpa_fallback_success(self):
        args = self._args()
        local = {
            "ok": False,
            "total": 2,
            "success": 0,
            "failed": 2,
            "results": [
                {"email": "a@example.com", "ok": False, "probe": {"status": "token_invalid"}},
                {"email": "b@example.com", "ok": False, "probe": {"status": "unknown"}},
            ],
        }
        fallback_result = {"ok": False, "success": 1, "failed": 1, "results": []}
        with (
            patch("sms_tool.accounts.account_recovery.refresh_local_quota_statuses", return_value=local),
            patch("sms_tool.cpa_import.refresh_cpa_quota_statuses", return_value=fallback_result) as fallback,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli._refresh_cpa_quota(args)

        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(fallback.call_args.kwargs["emails"], ["a@example.com", "b@example.com"])


class ReloginPanelNoteTests(unittest.TestCase):
    """The panel must explain why a 401 relogin did not run instead of going
    silent — a globally breaker-disabled recovery previously looked like an
    unexplained mass token_invalid."""

    @staticmethod
    def _note(relogin):
        from sms_tool.commands.accounts import _relogin_panel_note

        return _relogin_panel_note(relogin)

    def test_disabled_gate_explains_the_breaker(self):
        note = self._note({"ok": False, "mode": "disabled", "error": "mailbox_pool_repair_required"})
        self.assertIn("邮箱池熔断", note)
        self.assertIn("mailbox-pool-repaired", note)

    def test_terminal_deactivation_reports_the_drop(self):
        note = self._note({"ok": False, "mode": "chatgpt_email_otp", "error": "account_deactivated", "terminal": True})
        self.assertIn("已注销", note)
        self.assertIn("掉号", note)

    def test_cooldown_and_concurrency_are_distinguished(self):
        self.assertIn("冷却中", self._note({"ok": False, "mode": "cooldown", "error": "relogin_cooldown"}))
        self.assertIn("并发槽", self._note({"ok": False, "mode": "concurrency_limited", "error": "relogin_concurrency_limited"}))

    def test_generic_failure_includes_the_error(self):
        note = self._note({"ok": False, "mode": "chatgpt_email_otp", "error": "email_otp_timeout"})
        self.assertIn("重登失败", note)
        self.assertIn("email_otp_timeout", note)

    def test_success_and_absent_relogin_produce_no_note(self):
        self.assertEqual(self._note({"ok": True}), "")
        self.assertEqual(self._note({}), "")

    def test_summary_prints_relogin_note_and_drop_marker(self):
        from sms_tool.commands.accounts import _print_quota_summary

        result = {
            "ok": False,
            "results": [
                {
                    "email": "dead@example.com",
                    "ok": False,
                    "probe": {"ok": False, "status": "token_invalid", "dropped": "token_revoked"},
                    "relogin": {"ok": False, "mode": "disabled", "error": "mailbox_pool_repair_required"},
                },
                {
                    "email": "recovered@example.com",
                    "ok": True,
                    "probe": {"ok": True, "status": "active"},
                    "relogin": {"ok": True},
                },
            ],
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_quota_summary(result)
        output = buffer.getvalue()
        self.assertIn("邮箱池熔断", output)
        self.assertIn("已标记掉号", output)
        self.assertIn("重登成功，令牌已刷新", output)


class ProbeReasonLabelTests(unittest.TestCase):
    """The panel must show Chinese reasons, not raw backend strings."""

    @staticmethod
    def _label(raw):
        from sms_tool.commands.accounts import _probe_reason_label

        return _probe_reason_label(raw)

    def test_known_reasons_are_translated(self):
        self.assertEqual("账号已注销", self._label("account_deactivated"))
        self.assertEqual("AT 失效（HTTP 401）", self._label("token_invalid"))
        self.assertEqual("AT 失效（HTTP 401）", self._label("HTTP 401"))
        self.assertEqual("网络超时", self._label("curl: (28) Operation timed out"))
        self.assertEqual("代理连接失败", self._label("HTTPSConnectionPool: ProxyError"))
        self.assertEqual("邮箱链路失败", self._label("mailbox_transport"))

    def test_translation_is_case_insensitive(self):
        self.assertEqual("账号已注销", self._label("Account_Deactivated"))
        self.assertEqual("网络超时", self._label("CURL: (28) timed out"))

    def test_unknown_reason_keeps_a_truncated_tail(self):
        self.assertEqual("检测失败（weird_new_error）", self._label("weird_new_error"))

    def test_unknown_reason_is_truncated_so_html_cannot_flood_the_panel(self):
        label = self._label("x" * 400)
        self.assertTrue(label.startswith("检测失败（"))
        self.assertLess(len(label), 80)

    def test_empty_reason_falls_back_to_a_label(self):
        self.assertEqual("检测失败", self._label(""))
        self.assertEqual("检测失败", self._label(None))

    def test_liveness_summary_line_is_chinese(self):
        from sms_tool.commands.accounts import _print_quota_summary

        result = {
            "ok": False,
            "results": [
                {"email": "a@example.com", "ok": True, "probe": {"ok": True}},
                {
                    "email": "b@example.com",
                    "ok": False,
                    "probe": {"ok": False, "status": "token_invalid"},
                },
                {
                    "email": "c@example.com",
                    "ok": False,
                    "probe": {"ok": False, "status": "account_deactivated"},
                },
            ],
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_quota_summary(result)
        output = buffer.getvalue()
        # No raw English reason in the panel any more.
        self.assertNotIn("token_invalid", output)
        self.assertNotIn("Liveness check", output)
        self.assertIn("AT 失效（HTTP 401）", output)
        self.assertIn("账号已注销", output)
        self.assertIn("测活完成：共 3 个账号，正常 1，掉号 1，其他失败 1", output)

    def test_promotion_summary_prints_staged_chinese_lines(self):
        from sms_tool.commands.accounts import _print_promotion_summary

        result = {
            "ok": False,
            "total": 2,
            "results": [
                {"email": "a@example.com", "ok": True, "promotion_status": "可试用Plus-50%"},
                {"email": "b@example.com", "ok": False, "promotion_status": "AT失效"},
            ],
        }
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_promotion_summary(result)
        output = buffer.getvalue()
        self.assertIn("优惠检测完成：共 2 个账号，检测成功 1，失败 1", output)
        self.assertIn("[!] b@example.com: AT 失效（HTTP 401）", output)
        # Successful rows stay out of the panel; they live in the result dialog.
        self.assertNotIn("a@example.com", output)


if __name__ == "__main__":
    unittest.main()
