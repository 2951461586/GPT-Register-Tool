from types import SimpleNamespace
from unittest.mock import patch

from sms_tool.batch_runner import run_batch_impl


def test_internal_failure_does_not_write_proxy_health():
    mailbox = SimpleNamespace(email="internal@example.com")

    def run_email(**_kwargs):
        return {
            "success": False,
            "email": mailbox.email,
            "error": "registration_internal_error:NameError: get_device_context",
            "failure_class": "internal",
            "retryable": False,
        }

    with patch("sms_tool.batch_runner.CFG", {"email_registration": {}, "registration": {}}), \
         patch("sms_tool.batch_runner.ProxyHealthTracker") as tracker:
        results = run_batch_impl(
            count=1,
            proxy="http://proxy.invalid:8080",
            mailboxes=[mailbox],
            workers=1,
            max_attempts=1,
            retry_delay_seconds=0,
            run_email_func=run_email,
        )

    assert results[0]["failure_class"] == "internal"
    tracker.assert_not_called()
