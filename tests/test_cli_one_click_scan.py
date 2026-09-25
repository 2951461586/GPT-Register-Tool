import types
import unittest
from unittest.mock import patch

from sms_tool import cli


class OneClickScanCliTests(unittest.TestCase):
    def test_one_click_scan_disables_workspace_controls(self):
        args = types.SimpleNamespace(
            email="user@gmail.com",
            email_file="",
            session_file="",
            workers=3,
            proxy="socks5h://127.0.0.1:7897",
            refresh_timeout=90,
            no_scan_workspace_status=False,
            scan_switch_workspace_id="workspace-should-be-ignored",
            scan_fallback_workspace_ids="fallback-should-be-ignored",
            scan_auto_switch_workspace=True,
            scan_relogin_mode="web_session",
            quota_auto_relogin=True,
        )

        with patch("sms_tool.accounts.account_scan.scan_accounts", return_value={"failed": 0, "results": []}) as scan:
            cli._one_click_scan(args)

        kwargs = scan.call_args.kwargs
        self.assertFalse(kwargs["workspace_check"])
        self.assertEqual(kwargs["switch_workspace_id"], "")
        self.assertEqual(kwargs["fallback_workspace_ids"], [])
        self.assertFalse(kwargs["auto_switch_workspace"])
        self.assertTrue(kwargs["quota_relogin_on_401"])
        self.assertEqual(kwargs["relogin_mode"], "web_session")

    def test_one_click_scan_exits_nonzero_on_partial_persistence(self):
        args = types.SimpleNamespace(
            email="user@example.test", email_file="", session_file="", workers=1,
            proxy=None, refresh_timeout=20, quota_auto_relogin=False,
            scan_relogin_mode="auto", scan_deep_probe=False,
        )
        with patch(
            "sms_tool.accounts.account_scan.scan_accounts",
            return_value={"failed": 0, "persist_failed": 1, "alive": 1},
        ), self.assertRaises(SystemExit) as exit_code:
            cli._one_click_scan(args)
        self.assertEqual(exit_code.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
