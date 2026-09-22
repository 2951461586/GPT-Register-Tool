"""A registration batch must report its own outcome through the exit code.

``save_registration_results`` has always returned ``ok``/``failed``, but the
registration path in ``cli.main()`` used to fall off the end of the function.
A batch that registered 0 of 3 accounts printed
``[*] Done. 0/3 registered successfully`` and still exited 0, so every script,
CI step and wrapper that watches the process status read it as success.
(Restoring that shape -- mutator M1 in ``runtime/_mutate_registration_exit_code.py``
-- makes this file fail.  The old exit status was never observed on a live
batch, because reaching that tail for real costs email OTPs.)

Exit code 3 is the documented "runtime/provider failure" slot
(``docs/architecture.md``) and is the code ``--target-at200`` and
``--delete-account`` already raise for a batch that did not fully succeed.  The
defect was an asymmetry: the bounded ReMail path returned 3, the ordinary path
returned 0.

The end-to-end tests below drive ``main()`` with the *real* save step, so the
exit code is asserted against genuine report output rather than a hand-written
literal.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sms_tool import cli
from sms_tool.cli import _registration_exit_code

ROOT = Path(__file__).resolve().parents[1]


class _Mailbox:
    def __init__(self, email):
        self.email = email


def _source(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def _config():
    return {
        "proxy": {},
        "storage": {},
        "email_registration": {},
        "output": {"filename_pattern": "session_{email}_{timestamp}.json"},
    }


def _run_registration(tmp_path, results, *, count, extra_args=()):
    """Run the ordinary registration path end to end.

    Returns ``(exit_code, stdout)``.  ``run_batch`` is faked (the signup lane is
    not what is under test) but the report comes from the real
    ``save_registration_results``.
    """
    mailboxes = [_Mailbox(f"pool{index}@example.com") for index in range(count)]
    argv = [
        "sms_tool",
        "--email", "pool0@example.com",
        "--count", str(count),
        "--workers", "1",
        *extra_args,
    ]
    buffer = io.StringIO()
    # A successful registration enqueues post-registration health checks, which
    # start a daemon coordinator thread.  Left running it races interpreter
    # shutdown and prints a traceback after the run, so it is stubbed out here.
    health_queue = patch(
        "sms_tool.accounts.account_health_queue.enqueue_post_registration_checks",
        return_value=[],
    )
    with patch.object(cli, "CFG", _config()), \
         patch.object(sys, "argv", argv), \
         patch.object(cli, "_load_mailbox_pool", return_value=mailboxes), \
         patch.object(cli, "_preflight_registration_before_mailbox", return_value={"ok": True}), \
         patch.object(cli, "_registration_phone_pool", return_value=None), \
         patch.object(cli, "_proxy_pool_values", return_value=[]), \
         patch.object(cli, "run_batch", return_value=list(results)), \
         patch.object(cli, "upsert_account", return_value=True), \
         patch.object(cli, "database_path", return_value=tmp_path / "accounts.sqlite3"), \
         patch("sms_tool.storage.record_registration_audit"), \
         patch("sms_tool.batch_runner.get_account_records", return_value={}), \
         health_queue, \
         redirect_stdout(buffer):
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code, buffer.getvalue()
    return 0, buffer.getvalue()


def _failed(email):
    return {"success": False, "email": email, "error": "route_failed"}


def _succeeded(email):
    return {"success": True, "email": email, "access_token": "at-test"}


# --------------------------------------------------------------------------
# the exit code itself
# --------------------------------------------------------------------------

def test_the_exit_code_follows_the_report_failed_count():
    assert _registration_exit_code({"ok": True, "failed": 0}) == 0
    assert _registration_exit_code({"ok": False, "failed": 3}) == 3


def test_an_explicit_not_ok_verdict_exits_non_zero():
    # Belt and braces: the verdict alone is enough, even if a future report
    # forgets the count.
    assert _registration_exit_code({"ok": False, "failed": 0}) == 3


def test_the_failure_count_alone_is_enough_to_fail_the_batch():
    """A real report sets ``ok`` and ``failed`` consistently, so pin the count
    on its own.

    Without this, the verdict branch masks a relaxed threshold: the
    end-to-end partial-batch test would still pass against
    ``failed > 1`` because a 1-of-2 report also sets ``ok=False``.
    """
    assert _registration_exit_code({"success": 1, "total": 2, "failed": 1}) == 3


def test_a_report_without_a_verdict_is_left_at_zero():
    """A missing verdict is not evidence of failure."""
    assert _registration_exit_code({}) == 0
    assert _registration_exit_code(None) == 0
    assert _registration_exit_code([]) == 0
    assert _registration_exit_code({"failed": None}) == 0


def test_an_unparseable_failed_count_does_not_crash_the_process():
    assert _registration_exit_code({"failed": "not-a-number"}) == 0


def test_a_stringified_failed_count_still_exits_non_zero():
    assert _registration_exit_code({"failed": "2"}) == 3


# --------------------------------------------------------------------------
# end to end through main()
# --------------------------------------------------------------------------

def test_a_batch_that_registered_nothing_exits_three(tmp_path):
    """The defect shape: ``0/3 registered`` used to come with exit status 0.

    Restoring that shape (mutator M1) makes this file fail, which is how the
    old behaviour is pinned here -- the exit status was never observed on a
    live batch, because reaching this tail for real costs email OTPs.
    """
    code, output = _run_registration(
        tmp_path,
        [_failed(f"pool{index}@example.com") for index in range(3)],
        count=3,
    )

    assert "0/3 registered successfully" in output
    assert code == 3


def test_a_fully_successful_batch_exits_zero(tmp_path):
    code, output = _run_registration(
        tmp_path,
        [_succeeded(f"pool{index}@example.com") for index in range(2)],
        count=2,
    )

    assert "2/2 registered successfully" in output
    assert code == 0


def test_a_partially_failed_batch_exits_three(tmp_path):
    """``ok`` means *all* of them, so one failure is a failed batch."""
    code, output = _run_registration(
        tmp_path,
        [_succeeded("pool0@example.com"), _failed("pool1@example.com")],
        count=2,
    )

    assert "1/2 registered successfully" in output
    assert code == 3


def test_the_phone_lane_also_reports_a_failed_batch(tmp_path):
    """The phone lane had the identical shape, so it gets the same behaviour.

    ``run_phone_register`` is imported inside ``main()``, so the patch target is
    the module that defines it, not the ``cli`` namespace.
    """
    buffer = io.StringIO()
    argv = ["sms_tool", "--phone-register", "--count", "1"]
    with patch.object(cli, "CFG", _config()), \
         patch.object(sys, "argv", argv), \
         patch.object(cli, "_load_mailbox_pool", return_value=[_Mailbox("pool0@example.com")]), \
         patch.object(cli, "_preflight_registration_before_mailbox", return_value={"ok": True}), \
         patch.object(cli, "_registration_phone_pool", return_value=None), \
         patch.object(cli, "_proxy_pool_values", return_value=[]), \
         patch("sms_tool.registration.run_phone_register",
               return_value={"success": False, "phone": "+15550000000", "error": "sms_timeout"}), \
         patch.object(cli, "upsert_account", return_value=True), \
         patch.object(cli, "database_path", return_value=tmp_path / "accounts.sqlite3"), \
         patch("sms_tool.storage.record_registration_audit"), \
         redirect_stdout(buffer):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        else:
            code = 0

    assert "0/1 registered successfully" in buffer.getvalue()
    assert code == 3


def test_the_desktop_client_still_gets_the_payload_when_the_batch_fails(tmp_path):
    """The envelope is written before the process exits.

    ``BackendResultInterpreter.Interpret`` keeps ``result.Payload`` on a
    non-zero exit, but only because the envelope already reached stdout.
    Raising first would leave the WPF showing bare stderr.
    """
    report = {
        "ok": False,
        "batch_id": "batch",
        "total": 1,
        "success": 0,
        "failed": 1,
    }
    mailboxes = [_Mailbox("pool0@example.com")]
    argv = ["sms_tool", "--email", "pool0@example.com", "--count", "1", "--desktop-ipc"]
    with patch.object(cli, "CFG", _config()), \
         patch.object(sys, "argv", argv), \
         patch.object(cli, "_load_mailbox_pool", return_value=mailboxes), \
         patch.object(cli, "_preflight_registration_before_mailbox", return_value={"ok": True}), \
         patch.object(cli, "_registration_phone_pool", return_value=None), \
         patch.object(cli, "_proxy_pool_values", return_value=[]), \
         patch.object(cli, "run_batch", return_value=[_failed("pool0@example.com")]), \
         patch.object(cli, "_save_registration_results", return_value=report), \
         patch("sms_tool.batch_runner.get_account_records", return_value={}), \
         patch.object(cli, "emit_result") as emit, \
         redirect_stdout(io.StringIO()):
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 3
        else:
            raise AssertionError("a failed batch did not exit non-zero")

    emit.assert_called_once_with(report, enabled=True)


# --------------------------------------------------------------------------
# wiring guards
# --------------------------------------------------------------------------

def test_both_registration_tails_consult_the_report():
    """The phone lane had the same shape, so it gets the same guard.

    Without this, the fix is one deletion away from silently returning 0 again.
    The ordering check is what matters: the envelope has to be written *after*
    the report exists and *before* the process exits.
    """
    body = _source("sms_tool/cli.py").split("\ndef _registration_exit_code", 1)[0]
    tails = [
        index for index in range(len(body))
        if body.startswith("raise SystemExit(exit_code)", index)
    ]
    assert len(tails) == 2, "expected the phone lane and the mailbox lane to both exit on failure"
    for at in tails:
        emit_at = body.rindex("emit_result(report, enabled=True)", 0, at)
        save_at = body.rindex("report = _save_registration_results(", 0, at)
        assert save_at < emit_at < at, (
            "a registration tail raised without first writing the IPC envelope "
            "for its own report"
        )


def test_the_registration_path_uses_the_same_code_as_the_bounded_path():
    """The defect was an asymmetry; this keeps the two paths aligned."""
    assert "raise SystemExit(3)" in _source("sms_tool/commands/registration.py")
    assert _registration_exit_code({"ok": False, "failed": 1}) == 3
