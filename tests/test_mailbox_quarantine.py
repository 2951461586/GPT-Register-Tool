import json
import time
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


def test_mailbox_auth_invalid_quarantine_expires_after_cooldown(tmp_path):
    """A ``mailbox_auth_invalid`` entry must carry a real deadline.

    The legacy ``retry_after = 0`` meant "active forever", so one transient
    icloud 401 froze OTP recovery for the entire pool until an operator
    acknowledged a repair.
    """
    path = tmp_path / "mailbox_auth_quarantine.json"
    dead = MailboxAccount(
        email="dead@icloud.com",
        provider="icloud_url",
        token="https://mail.example/dead-secret",
    )
    also_dead = MailboxAccount(
        email="also@icloud.com",
        provider="icloud_url",
        token="https://mail.example/also-secret",
    )
    with patch("sms_tool.mailbox_quarantine.quarantine_path", return_value=path):
        fingerprint = record_mailbox_auth_invalid(dead, cooldown_seconds=60)
        # The *default* must be bounded too: an explicit cooldown in the call
        # above would otherwise mask a regression back to ``0`` == forever.
        default_fingerprint = record_mailbox_auth_invalid(also_dead)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["entries"][default_fingerprint]["retry_after"] > 0
        assert saved["entries"][fingerprint]["retry_after"] > 0
        assert not mailbox_relogin_allowed()

        for key in (fingerprint, default_fingerprint):
            saved["entries"][key]["retry_after"] = time.time() - 1
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        assert mailbox_relogin_allowed()


def test_single_quarantined_credential_does_not_block_other_accounts(tmp_path):
    """One dead credential must not take the whole batch down with it.

    Observed as 17 of 25 accounts returning ``mailbox_pool_repair_required``
    while the quarantine held a single entry.
    """
    path = tmp_path / "mailbox_auth_quarantine.json"
    dead = MailboxAccount(
        email="dead@icloud.com",
        provider="icloud_url",
        token="https://mail.example/dead-secret",
    )
    with patch("sms_tool.mailbox_quarantine.quarantine_path", return_value=path):
        record_mailbox_auth_invalid(dead)
        assert mailbox_relogin_allowed("healthy@outlook.com")
        # The account that owns the dead mailbox is still failed fast.
        assert not mailbox_relogin_allowed("dead@icloud.com")
        # Legacy global call keeps its old all-or-nothing semantics.
        assert not mailbox_relogin_allowed()


def test_systemic_quarantine_still_blocks_the_batch(tmp_path):
    """The pool-wide panic button survives, but needs a real blast radius."""
    path = tmp_path / "mailbox_auth_quarantine.json"
    with patch("sms_tool.mailbox_quarantine.quarantine_path", return_value=path):
        for index in range(3):
            record_mailbox_auth_invalid(
                MailboxAccount(
                    email=f"dead{index}@icloud.com",
                    provider="icloud_url",
                    token=f"https://mail.example/secret-{index}",
                )
            )
        assert not mailbox_relogin_allowed("healthy@outlook.com")


def test_per_account_breaker_only_blocks_matching_email():
    """The ReMail dead-registry must only block the specific account whose
    mailbox credential failed, not every account in the batch."""
    dead_entries = [
        {"email": "bad@outlook.com", "reason": "mailbox_auth_invalid", "last_seen_at": 9999},
    ]
    with (
        patch("sms_tool.mailbox_quarantine._read", return_value={}),
        patch("sms_tool.mailbox_remail._read_dead_remail_registry", return_value=dead_entries),
    ):
        # The account whose mailbox is bad is blocked.
        assert not mailbox_relogin_allowed("bad@outlook.com")
        # A different account is not collateral-blocked.
        assert mailbox_relogin_allowed("other@outlook.com")
        # Legacy global call (no email) still blocks.
        assert not mailbox_relogin_allowed()


def test_per_account_breaker_ignores_non_matching_reason():
    """account_deactivated entries in the dead-registry must not trigger the
    mailbox-auth breaker."""
    dead_entries = [
        {"email": "deactivated@outlook.com", "reason": "account_deactivated", "last_seen_at": 9999},
    ]
    with (
        patch("sms_tool.mailbox_quarantine._read", return_value={}),
        patch("sms_tool.mailbox_remail._read_dead_remail_registry", return_value=dead_entries),
    ):
        assert mailbox_relogin_allowed("deactivated@outlook.com")
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
