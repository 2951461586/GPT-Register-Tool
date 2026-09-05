import json
from pathlib import Path
from unittest.mock import patch

from sms_tool.mailbox_quarantine import (
    filter_quarantined_mailboxes,
    mailbox_relogin_allowed,
    prune_quarantine_against_pool,
    record_mailbox_auth_invalid,
)
from sms_tool.mailbox_types import MailboxAccount
from sms_tool import mailbox_strategies
from sms_tool.providers.mailbox_graph import MailboxAuthInvalidError


def test_quarantine_uses_fingerprint_and_filters_only_same_credential(tmp_path):
    path = tmp_path / "mailbox_auth_quarantine.json"
    dead = MailboxAccount(
        email="dead@icloud.com",
        provider="icloud_url",
        token="https://mail.example/dead-secret",
    )
    replacement = MailboxAccount(
        email="dead@icloud.com",
        provider="icloud_url",
        token="https://mail.example/replacement-secret",
    )
    with patch("sms_tool.mailbox_quarantine.quarantine_path", return_value=path):
        fingerprint = record_mailbox_auth_invalid(dead)
        assert len(fingerprint) == 64
        assert not mailbox_relogin_allowed()
        assert filter_quarantined_mailboxes([dead, replacement]) == [replacement]

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert fingerprint in saved["entries"]
    assert "dead-secret" not in path.read_text(encoding="utf-8")

    with patch("sms_tool.mailbox_quarantine.quarantine_path", return_value=path):
        assert prune_quarantine_against_pool([replacement]) == 1
        assert mailbox_relogin_allowed()


def test_icloud_poll_stops_on_auth_invalid_without_retrying():
    mailbox = MailboxAccount(
        email="dead@icloud.com",
        provider="icloud_url",
        token="https://mail.example/dead-secret",
    )
    with patch(
        "sms_tool.mailbox._latest_email_otp_candidate",
        side_effect=MailboxAuthInvalidError(mailbox.email, "invalid"),
    ) as fetch:
        try:
            mailbox_strategies._icloud_poll_otp(mailbox, timeout=300)
        except MailboxAuthInvalidError:
            pass
        else:
            raise AssertionError("mailbox auth failure must terminate OTP polling")
    fetch.assert_called_once()
