"""A failed registration must not cost a second mailbox.

The persistence branch cannot write this record: ``outcome._failure_result``
sanitizes the result, so ``data["mailbox"]["token"]`` is already ``[REDACTED]``
by the time ``persist_*_result`` sees it. These tests pin the consequence -- the
credential that lands in the ledger is the **runtime** one, and a line built from
the result dict's placeholder would be refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sms_tool.mailbox import reclaim
from sms_tool.mailbox.parsers import parse_mailbox_pool_line
from sms_tool.registration.result import record_failed_mailbox_purchase


@dataclass
class _Mailbox:
    email: str = "buyer@example.com"
    token: str = "svc-token-raw"
    provider: str = "remail"
    order_no: str = ""
    purchase_id: str = ""


def _workflow(mailbox=_Mailbox()):
    return SimpleNamespace(runtime=SimpleNamespace(mailbox=mailbox), config=None)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the real recorder at a temp file so the wiring is exercised end to end."""
    target = tmp_path / "mailbox_reclaim.txt"
    monkeypatch.setattr(reclaim, "reclaim_pool_path", lambda config=None: target)
    return target


def test_an_auth_state_failure_keeps_the_purchased_mailbox(ledger):
    status = record_failed_mailbox_purchase(
        _workflow(),
        {"failure_class": "auth_state", "error": "create_account_failed:invalid_state"},
    )

    assert status == reclaim.STATUS_RECORDED
    lines = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    parsed = parse_mailbox_pool_line(lines[0], ledger, 1)
    assert parsed is not None
    assert parsed.provider == "remail"
    assert parsed.token == "svc-token-raw", "the ledger must hold the raw supplier token, not the sanitized copy"


def test_the_raw_runtime_token_wins_over_the_sanitized_result(ledger):
    """The result carries ``[REDACTED]``; the record must not copy it.

    This is the trap: ``persist_*_result`` only ever sees the sanitized dict, so
    a record written there would parse as a mailbox whose password is
    ``[REDACTED]`` and fail at OTP time -- long after the operator was told the
    mailbox had been kept.
    """
    result = {
        "failure_class": "auth_state",
        "error": "create_account_failed:invalid_state",
        "mailbox": {"email": "buyer@example.com", "token": "[REDACTED]"},
    }

    record_failed_mailbox_purchase(_workflow(), result)

    content = ledger.read_text(encoding="utf-8")
    assert "[REDACTED]" not in content
    assert "svc-token-raw" in content


def test_a_deactivated_account_does_not_reclaim_its_mailbox(ledger):
    """The address belongs to a live (but dead) account -- re-registering it is wrong."""
    status = record_failed_mailbox_purchase(
        _workflow(),
        {"failure_class": "account", "error": "registration_failed:account_deactivated"},
    )

    assert status == reclaim.STATUS_SKIPPED_EXHAUSTED
    assert not ledger.exists()


def test_an_already_registered_address_is_not_reclaimed(ledger):
    status = record_failed_mailbox_purchase(
        _workflow(),
        {"failure_class": "account", "error": "create_account_failed:user_already_exists"},
    )

    assert status == reclaim.STATUS_SKIPPED_EXHAUSTED
    assert not ledger.exists()


@pytest.mark.parametrize("workflow", [None, SimpleNamespace(runtime=None), SimpleNamespace()])
def test_a_workflow_without_a_mailbox_is_a_no_op(ledger, workflow):
    assert record_failed_mailbox_purchase(workflow, {"failure_class": "auth_state"}) == (
        reclaim.STATUS_SKIPPED_NO_MAILBOX
    )
    assert not ledger.exists()


def test_the_helper_never_raises_on_a_broken_workflow():
    """Bookkeeping must never escalate into a crashed registration."""
    broken = SimpleNamespace(runtime=SimpleNamespace(mailbox=object()), config=None)

    status = record_failed_mailbox_purchase(broken, {"failure_class": "auth_state"})

    # ``object()`` has neither a provider nor a token, so the recorder refuses it
    # (provider is checked first). What matters here is that it *returns*, not
    # raises: a bookkeeping miss must never abort the failure path.
    assert status in {
        reclaim.STATUS_SKIPPED_NO_TOKEN,
        reclaim.STATUS_SKIPPED_PROVIDER,
        reclaim.STATUS_ERROR,
    }
