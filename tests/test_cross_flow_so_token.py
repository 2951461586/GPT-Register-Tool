"""The existing-login lane must not replay a foreign-flow Sentinel SO (F2).

``create_account`` mints the ``oauth_create_account`` Sentinel pair and
``registration_sentinel_stages.issue_sentinel`` writes its SO into
``runtime.sentinel_so_token``.  The existing-login lane is handed that value as
its *fallback* SO and uses it on ``email-otp/send`` whenever its own
``authorize_continue`` mint returns no SO.  An SO is bound to the flow that
minted it (both producers write ``flow`` into the JSON object), so that is a
cross-flow reuse, not a fallback.

Two things are pinned here, and both are needed:

* the pure guard (``so_token_flow`` / ``same_flow_so_token``), including the
  deliberate decision to keep an *undeclared* legacy token; and
* the lane entry actually applies it -- a guard that is never called is
  decoration.
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from sms_tool.auth_flow import login, steps

AUTHORIZE_CONTINUE = steps.AUTHORIZE_CONTINUE_FLOW


def _so(flow: str) -> str:
    return json.dumps({"so": "so-value", "c": "c-value", "id": "did", "flow": flow})


# --------------------------------------------------------------------------
# 1) The pure guard
# --------------------------------------------------------------------------


def test_so_token_flow_reads_the_declared_flow():
    assert steps.so_token_flow(_so("oauth_create_account")) == "oauth_create_account"
    assert steps.so_token_flow(_so("authorize_continue")) == "authorize_continue"


@pytest.mark.parametrize("token", ["", None, "not json", "[1,2,3]", json.dumps({"so": "x"})])
def test_so_token_flow_refuses_to_guess(token):
    """Unreadable / flow-less SOs answer ``""`` (unknown), never a guess."""
    assert steps.so_token_flow(token) == ""


def test_a_declared_foreign_flow_is_dropped():
    assert steps.same_flow_so_token(_so("oauth_create_account"), AUTHORIZE_CONTINUE) == ""


def test_a_matching_flow_is_kept_verbatim():
    token = _so("authorize_continue")
    assert steps.same_flow_so_token(token, AUTHORIZE_CONTINUE) == token


@pytest.mark.parametrize("token", ["", None, "not json", json.dumps({"so": "x"})])
def test_an_undeclared_token_is_kept(token):
    """The guard stops a *declared* mismatch only.

    Dropping an undeclared legacy token would change behaviour on evidence we do
    not have -- the same rule ``otp_dispatch_verdict`` follows.
    """
    assert steps.same_flow_so_token(token, AUTHORIZE_CONTINUE) == token


# --------------------------------------------------------------------------
# 2) The lane entry applies it
# --------------------------------------------------------------------------


def _run_lane(so_token):
    """Drive ``_login_existing_account_with_email_otp`` with every phase stubbed.

    Returns the ``sentinel_so_token`` the OTP phase was handed.
    """
    signin = Mock(return_value=(None, {"csrf_token": "csrf", "current_url": "https://auth.openai.com/log-in"}))
    continue_ = Mock(
        return_value=(
            None,
            {
                "current_url": "https://auth.openai.com/email-verification",
                "continue_payload": {},
                "continue_next_url": "",
                "fresh_token": "fresh-main",
                "fresh_so": "",
            },
        )
    )
    probe = Mock(return_value=(None, {}))
    otp_phase = Mock(return_value=({"ok": True}, None))
    with (
        patch.object(login, "_existing_login_signin", signin),
        patch.object(login, "_existing_login_continue", continue_),
        patch.object(login, "_existing_login_probe", probe),
        patch.object(login.otp, "_existing_login_otp", otp_phase),
    ):
        result = login._login_existing_account_with_email_otp(
            session=Mock(),
            username="probe@example.com",
            mailbox=Mock(),
            did="did",
            session_logging_id="log",
            auth_base="https://auth.openai.com",
            chat_base="https://chatgpt.com",
            base_headers={},
            csrf_token="csrf",
            sentinel_so_token=so_token,
        )
    assert result == {"ok": True}
    # ``sentinel_so_token`` is the 8th positional argument of
    # ``_existing_login_otp`` (session, mailbox, did, auth_base, base_headers,
    # proxy, sentinel_token, sentinel_so_token, ...).
    return otp_phase.call_args.args[7]


def test_the_lane_drops_the_create_account_so():
    assert _run_lane(_so("oauth_create_account")) == ""


def test_the_lane_keeps_its_own_flow_so():
    assert _run_lane(_so("authorize_continue")) == _so("authorize_continue")


def test_the_lane_keeps_an_undeclared_so():
    assert _run_lane("legacy-raw-so") == "legacy-raw-so"
