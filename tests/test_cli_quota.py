import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli


class CliQuotaTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
