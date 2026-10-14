"""Offline tests for ``--payment-relogin-mode`` (AT recovery strategy on 401).

Background: the ``auto`` recovery chain falls through to ``chatgpt_email_otp``,
which needs a mailbox and hung a real batch for >30 min. ``web_session``
(session-cookie replay) is the strategy the recovery module itself documents as
the only production-success path on this fleet, so operators need a way to pin
it. The CLI flag is carried to the in-process JIT gate via
``PAYMENT_RELOGIN_MODE`` so the batch layer needs no extra parameter.
"""

from __future__ import annotations

import argparse
import os
from unittest.mock import patch

import pytest

from sms_tool import payment_auth
from sms_tool import cli


@pytest.fixture(autouse=True)
def _restore_relogin_env():
    """The code under test mutates ``os.environ`` directly; restore it."""
    saved = os.environ.get("PAYMENT_RELOGIN_MODE")
    yield
    if saved is None:
        os.environ.pop("PAYMENT_RELOGIN_MODE", None)
    else:
        os.environ["PAYMENT_RELOGIN_MODE"] = saved


def test_cli_exposes_relogin_mode_choices():
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--extract-payment-link", "--payment-method", "upi", "--payment-relogin-mode", "web_session"]
    )
    assert args.payment_relogin_mode == "web_session"
    assert parser.parse_args(["--extract-payment-link", "--payment-method", "upi"]).payment_relogin_mode is None


def test_cli_rejects_unknown_relogin_mode():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--extract-payment-link", "--payment-relogin-mode", "otp_magic"])


def _invoke_jit(**kwargs):
    """Run one 401->relogin pass and return the mode handed to relogin."""
    seen: dict[str, str] = {}

    def fake_relogin(account, proxy=None, timeout=180, mode="auto"):
        seen["mode"] = mode
        return {"ok": False, "error": "stop_after_capture"}

    with (
        patch("sms_tool.accounts.account_recovery.relogin_codex_account", side_effect=fake_relogin),
        patch("sms_tool.accounts.account_recovery.is_permanently_deactivated", return_value=False),
        patch("sms_tool.accounts.account_liveness.probe_account_liveness", return_value={"status_code": 401}),
        patch("sms_tool.payment_auth.load_account_seed", return_value=({"email": "e", "access_token": "at"}, "p")),
        patch("sms_tool.payment_auth.access_token_telemetry", return_value={}),
    ):
        payment_auth.ensure_payment_access_token(email="e", proxy="p", **kwargs)
    return seen.get("mode")


def test_explicit_mode_wins(monkeypatch):
    monkeypatch.setenv("PAYMENT_RELOGIN_MODE", "codex_oauth")
    assert _invoke_jit(relogin_mode="web_session") == "web_session"


def test_env_mode_is_honoured(monkeypatch):
    monkeypatch.setenv("PAYMENT_RELOGIN_MODE", "web_session")
    assert _invoke_jit() == "web_session"


def test_default_mode_is_auto(monkeypatch):
    monkeypatch.delenv("PAYMENT_RELOGIN_MODE", raising=False)
    assert _invoke_jit() == "auto"


class _Context:
    def payment_method(self, args):
        return "upi"

    def read_email_file(self, path):
        return ["a@example.com"]


def _run_extract(args):
    from sms_tool.commands import payment as payment_commands

    with (
        patch.object(payment_commands, "resolve_payment_route", return_value={"ok": False, "error": "stop"}),
        patch("sms_tool.desktop_ipc.emit_result"),
    ):
        with pytest.raises(SystemExit):
            payment_commands.extract_payment_link(args, _Context())


def test_extract_payment_link_sets_env_from_flag(monkeypatch):
    monkeypatch.delenv("PAYMENT_RELOGIN_MODE", raising=False)
    args = argparse.Namespace(
        payment_relogin_mode="web_session",
        desktop_ipc=False,
        email_file="emails.txt",
        email=None,
        payment_method="upi",
    )
    _run_extract(args)
    assert os.environ.get("PAYMENT_RELOGIN_MODE") == "web_session"


def test_extract_payment_link_clears_env_when_unset(monkeypatch):
    monkeypatch.setenv("PAYMENT_RELOGIN_MODE", "web_session")
    args = argparse.Namespace(
        payment_relogin_mode=None,
        desktop_ipc=False,
        email_file="emails.txt",
        email=None,
        payment_method="upi",
    )
    _run_extract(args)
    assert "PAYMENT_RELOGIN_MODE" not in os.environ
