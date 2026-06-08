import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli
from sms_tool.import_targets import normalize_import_target, target_label


def _args(target="cpa"):
    return SimpleNamespace(
        email_file="",
        email="",
        session_file="",
        import_target=target,
        codex_export_dir="",
        workers=4,
        no_session_refresh=True,
        proxy=None,
        refresh_timeout=60,
        cpa_api_url="",
        cpa_api_token="",
        sub2api_url="",
        sub2api_token="",
        sub2api_email="",
        sub2api_password="",
        sub2api_group="",
        sub2api_group_ids="",
        sub2api_proxy="",
        sub2api_proxy_id=None,
        sub2api_priority=None,
        sub2api_concurrency=None,
    )


class CliImportTests(unittest.TestCase):
    def test_one_click_import_uses_all_access_token_accounts_not_only_paid(self):
        rows = [
            {"email": "registered@example.com", "access_token": "at_1", "paypal_status": ""},
            {"email": "pending@example.com", "access_token": "at_2", "paypal_status": "link_ready"},
            {"email": "paid@example.com", "access_token": "at_3", "paypal_status": "completed"},
            {"email": "failed-no-token@example.com", "access_token": "", "paypal_status": "completed"},
        ]

        with patch.object(cli, "list_paypal_accounts", return_value=rows):
            with patch("sms_tool.import_targets.import_account_sessions", return_value={
                "ok": True,
                "total": 3,
                "success": 3,
                "failed": 0,
                "results": [],
            }) as imported:
                cli._import_cpa(_args("cpa"))

        imported.assert_called_once()
        self.assertEqual(imported.call_args.args[0], "cpa")
        self.assertEqual(imported.call_args.args[1], [
            "registered@example.com",
            "pending@example.com",
            "paid@example.com",
        ])

    def test_one_click_import_supports_sub2api_target(self):
        rows = [{"email": "registered@example.com", "access_token": "at_1", "paypal_status": ""}]

        with patch.object(cli, "list_paypal_accounts", return_value=rows):
            with patch("sms_tool.import_targets.import_account_sessions", return_value={
                "ok": True,
                "total": 1,
                "success": 1,
                "failed": 0,
                "results": [],
            }) as imported:
                cli._import_cpa(_args("sub2api"))

        imported.assert_called_once()
        self.assertEqual(imported.call_args.args[0], "sub2api")
        self.assertEqual(imported.call_args.args[1], ["registered@example.com"])

    def test_import_target_supports_cliproxyapi_alias(self):
        self.assertEqual(normalize_import_target("cliproxyapi"), "cliproxyapi")
        self.assertEqual(target_label("cliproxyapi"), "CLIProxyAPI")


if __name__ == "__main__":
    unittest.main()
