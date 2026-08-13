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
            "quota_relogin_timeout": 180,
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
            quota_relogin_timeout=180,
            scan_relogin_mode="auto",
            cpa_api_url=None,
            cpa_api_token=None,
        )
        result = {"ok": True, "total": 1, "success": 1, "failed": 0, "results": []}
        with patch("sms_tool.account_recovery.refresh_local_quota_statuses", return_value=result) as refresh:
            with redirect_stdout(io.StringIO()):
                cli._refresh_cpa_quota(args)

        self.assertTrue(refresh.call_args.kwargs["relogin_on_401"])
        self.assertEqual(refresh.call_args.kwargs["relogin_timeout"], 180)
        self.assertEqual(refresh.call_args.kwargs["relogin_mode"], "auto")

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
            patch("sms_tool.account_recovery.refresh_local_quota_statuses", return_value=local),
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
            patch("sms_tool.account_recovery.refresh_local_quota_statuses", return_value=local),
            patch("sms_tool.cpa_import.refresh_cpa_quota_statuses", return_value=fallback_result) as fallback,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                cli._refresh_cpa_quota(args)

        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(fallback.call_args.kwargs["emails"], ["a@example.com", "b@example.com"])


if __name__ == "__main__":
    unittest.main()
