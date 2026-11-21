import types
import unittest
from unittest.mock import patch

from sms_tool import cli


class OneClickScanCliTests(unittest.TestCase):
    def test_one_click_scan_forwards_relogin_options_without_workspace_controls(self):
        args = types.SimpleNamespace(
            email="user@gmail.com",
            email_file="",
            session_file="",
            workers=3,
            proxy="socks5h://127.0.0.1:7897",
            refresh_timeout=90,
            scan_relogin_mode="web_session",
            quota_auto_relogin=True,
        )

        with patch("sms_tool.accounts.account_scan.scan_accounts", return_value={"failed": 0, "results": []}) as scan:
            cli._one_click_scan(args)

        kwargs = scan.call_args.kwargs
        self.assertNotIn("workspace_check", kwargs)
        self.assertNotIn("switch_workspace_id", kwargs)
        self.assertNotIn("fallback_workspace_ids", kwargs)
        self.assertNotIn("auto_switch_workspace", kwargs)
        self.assertTrue(kwargs["quota_relogin_on_401"])
        self.assertEqual(kwargs["relogin_mode"], "web_session")

    def test_one_click_scan_exits_nonzero_on_partial_persistence(self):
        args = types.SimpleNamespace(
            email="user@example.test",
            email_file="",
            session_file="",
            workers=1,
            proxy=None,
            refresh_timeout=20,
            quota_auto_relogin=False,
            scan_relogin_mode="auto",
            scan_deep_probe=False,
        )
        with (
            patch(
                "sms_tool.accounts.account_scan.scan_accounts",
                return_value={"failed": 0, "persist_failed": 1, "alive": 1},
            ),
            self.assertRaises(SystemExit) as exit_code,
        ):
            cli._one_click_scan(args)
        self.assertEqual(exit_code.exception.code, 3)


if __name__ == "__main__":
    unittest.main()


class OneClickScanFlagSurfaceTests(unittest.TestCase):
    """The four ``--scan-*workspace*`` flags were accepted and ignored.

    ``_one_click_scan`` hard-codes "no workspace probing" (asserted above), so
    the flags were removed on 2026-11-20 as a dead CLI contract: parsing a flag
    nobody reads is a promise the tool cannot keep. Re-adding one is now a
    decision that shows up here.
    """

    def test_deprecated_scan_workspace_flags_are_gone(self) -> None:
        parser = cli.build_parser()
        option_strings = {option for action in parser._actions for option in action.option_strings}
        removed = {
            "--no-scan-workspace-status",
            "--scan-switch-workspace-id",
            "--scan-fallback-workspace-ids",
            "--scan-auto-switch-workspace",
        }
        self.assertEqual(option_strings & removed, set())

    def test_removed_flags_are_rejected_instead_of_silently_ignored(self) -> None:
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--one-click-scan", "--scan-switch-workspace-id", "ws-1"])
