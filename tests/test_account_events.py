from unittest.mock import patch

from sms_tool.account_events import notify_account_deactivated


def test_storage_event_dispatches_only_for_remail():
    with patch("sms_tool.providers.mailbox_remail.record_dead_remail_account") as record:
        notify_account_deactivated({"email": "a@example.test", "mailbox_provider": "remail"})
    record.assert_called_once()


def test_storage_event_ignores_other_providers():
    with patch("sms_tool.providers.mailbox_remail.record_dead_remail_account") as record:
        notify_account_deactivated({"email": "a@example.test", "mailbox_provider": "gmail"})
    record.assert_not_called()
