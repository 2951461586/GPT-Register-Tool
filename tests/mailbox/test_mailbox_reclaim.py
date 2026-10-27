"""The failed-run mailbox ledger: a paid order must survive its run.

A ReMail mailbox is charged at purchase time. Measured 2026-10-26: a three
account batch bought three iCloud mailboxes and produced two accounts; the third
died at ``create_account`` with ``409 invalid_state`` and could not be retried
without paying again, because nothing kept its supplier token.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sms_tool.mailbox import reclaim
from sms_tool.mailbox.parsers import parse_mailbox_pool_line


@dataclass
class _Mailbox:
    """Stand-in for ``MailboxAccount`` -- only the fields the ledger reads."""

    email: str = "buyer@example.com"
    token: str = "svc-token-123"
    provider: str = "remail"
    order_no: str = ""
    purchase_id: str = ""


def test_the_line_round_trips_through_the_real_pool_parser(tmp_path):
    """The ledger must be feedable straight back with ``--mailbox-file``.

    Round-tripping through ``parse_mailbox_pool_line`` is what proves that: a
    format we invent here would be silently skipped by the router, so the record
    would look written and be useless.
    """
    mailbox = _Mailbox(order_no="ORDER-9", purchase_id="42")

    line = reclaim.format_reclaim_line(mailbox)
    parsed = parse_mailbox_pool_line(line, tmp_path / "mailbox_reclaim.txt", 1)

    assert line.startswith("remail://buyer@example.com---svc-token-123---")
    assert parsed is not None
    assert parsed.provider == "remail"
    assert parsed.email == "buyer@example.com"
    assert parsed.token == "svc-token-123"
    assert parsed.order_no == "ORDER-9"
    assert parsed.purchase_id == "42"


def test_the_legacy_two_column_form_is_used_when_the_order_identity_is_unknown(tmp_path):
    line = reclaim.format_reclaim_line(_Mailbox())
    assert line == "remail://buyer@example.com---svc-token-123"
    parsed = parse_mailbox_pool_line(line, tmp_path / "p.txt", 1)
    assert parsed is not None and parsed.token == "svc-token-123"


def test_recording_is_idempotent_per_email(tmp_path):
    target = tmp_path / "mailbox_reclaim.txt"
    mailbox = _Mailbox()

    assert reclaim.record_reclaimable_mailbox(mailbox, failure_class="auth_state", path=target) == (
        reclaim.STATUS_RECORDED
    )
    assert reclaim.record_reclaimable_mailbox(mailbox, failure_class="auth_state", path=target) == (
        reclaim.STATUS_DUPLICATE
    )
    assert target.read_text(encoding="utf-8").count("buyer@example.com") == 1


def test_a_second_distinct_mailbox_is_appended_on_its_own_line(tmp_path):
    target = tmp_path / "mailbox_reclaim.txt"
    reclaim.record_reclaimable_mailbox(_Mailbox(email="a@example.com"), failure_class="network", path=target)
    reclaim.record_reclaimable_mailbox(_Mailbox(email="b@example.com"), failure_class="network", path=target)

    lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0].startswith("remail://a@example.com")
    assert lines[1].startswith("remail://b@example.com")


@pytest.mark.parametrize("failure_class", sorted(reclaim.RECLAIMABLE_FAILURE_CLASSES))
def test_failures_that_leave_the_mailbox_spendable_are_recorded(tmp_path, failure_class):
    target = tmp_path / "mailbox_reclaim.txt"
    status = reclaim.record_reclaimable_mailbox(_Mailbox(), failure_class=failure_class, path=target)
    assert status == reclaim.STATUS_RECORDED
    assert target.is_file()


@pytest.mark.parametrize("failure_class", ["account", "otp", "mailbox"])
def test_failures_that_spend_the_address_are_not_recorded(tmp_path, failure_class):
    target = tmp_path / "mailbox_reclaim.txt"
    status = reclaim.record_reclaimable_mailbox(_Mailbox(), failure_class=failure_class, path=target)
    assert status == reclaim.STATUS_SKIPPED_EXHAUSTED
    assert not target.exists()


@pytest.mark.parametrize(
    "error",
    [
        "create_account_failed:user_already_exists",
        "registration_failed:account_deactivated",
        "identity_provider_mismatch",
        "partial_registered",
        "phone_already_registered_or_login_redirect",
        "mailbox_token_expired",
        "invalid_grant",
    ],
)
def test_an_exhausted_address_is_never_offered_again(tmp_path, error):
    """Re-offering these burns another OTP round and can never succeed."""
    target = tmp_path / "mailbox_reclaim.txt"
    status = reclaim.record_reclaimable_mailbox(_Mailbox(), error=error, path=target)
    assert status == reclaim.STATUS_SKIPPED_EXHAUSTED
    assert not target.exists()


def test_the_failure_class_is_derived_when_the_caller_does_not_pass_one(tmp_path):
    """``finalize`` knows the error string but not always the class."""
    target = tmp_path / "mailbox_reclaim.txt"
    status = reclaim.record_reclaimable_mailbox(
        _Mailbox(),
        error="create_account_failed:invalid_state: Your sign-in session is no longer valid",
        path=target,
    )
    assert status == reclaim.STATUS_RECORDED


def test_a_redacted_credential_is_never_written(tmp_path):
    """The sanitized failure result carries ``[REDACTED]`` -- that is not a token.

    This is the trap the ledger exists to avoid: a line with a placeholder would
    parse fine and then fail at OTP time, long after the operator believed the
    mailbox was kept.
    """
    target = tmp_path / "mailbox_reclaim.txt"
    for token in ("[REDACTED]", "***"):
        status = reclaim.record_reclaimable_mailbox(_Mailbox(token=token), failure_class="auth_state", path=target)
        assert status == reclaim.STATUS_SKIPPED_NO_TOKEN
    assert not target.exists()


@pytest.mark.parametrize("provider", ["icloud", "outlook", "chatai", ""])
def test_only_providers_whose_line_carries_a_token_are_recorded(tmp_path, provider):
    target = tmp_path / "mailbox_reclaim.txt"
    status = reclaim.record_reclaimable_mailbox(_Mailbox(provider=provider), failure_class="auth_state", path=target)
    assert status == reclaim.STATUS_SKIPPED_PROVIDER
    assert not target.exists()


def test_an_empty_mailbox_is_not_recorded(tmp_path):
    target = tmp_path / "mailbox_reclaim.txt"
    assert reclaim.record_reclaimable_mailbox(None, failure_class="auth_state", path=target) == (
        reclaim.STATUS_SKIPPED_NO_MAILBOX
    )
    assert reclaim.record_reclaimable_mailbox(_Mailbox(email=""), failure_class="auth_state", path=target) == (
        reclaim.STATUS_SKIPPED_NO_TOKEN
    )


def test_the_ledger_can_be_disabled(monkeypatch, tmp_path):
    target = tmp_path / "mailbox_reclaim.txt"
    monkeypatch.setenv("MAILBOX_RECLAIM_RECORD", "0")

    status = reclaim.record_reclaimable_mailbox(_Mailbox(), failure_class="auth_state", path=target)

    assert status == reclaim.STATUS_DISABLED
    assert not target.exists()


def test_a_write_failure_is_reported_not_raised(tmp_path):
    """Bookkeeping must never turn a failed registration into a crashed one."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "mailbox_reclaim.txt"

    status = reclaim.record_reclaimable_mailbox(_Mailbox(), failure_class="auth_state", path=target)

    assert status == reclaim.STATUS_ERROR


def test_reclaimed_emails_ignores_comments_and_blank_lines(tmp_path):
    target = tmp_path / "mailbox_reclaim.txt"
    target.write_text(
        "# operator note\n\nremail://a@example.com---tok\nremail://B@EXAMPLE.COM---tok2\n",
        encoding="utf-8",
    )
    assert reclaim.reclaimed_emails(target) == {"a@example.com", "b@example.com"}
