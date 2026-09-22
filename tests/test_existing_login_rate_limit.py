"""A 429 from the existing-login OTP send must open the batch rate-limit circuit.

The signup lane already answers this exact 429 with the process-wide
registration rate-limit circuit: ``registration_handlers._prepare_signup_auth_state``
reads ``retry_after_seconds``, calls ``mark_registration_rate_limited`` and aborts
with ``registration_rate_limited:retry_after=Ns``.  The existing-login lane
collapsed it to a bare status code instead, which classified ``unknown`` and left
the batch marching.

Observed 2026-09-14, one run (``runtime/logs/backend_stdout.log``)::

    Existing account OTP send: /api/accounts/email-otp/resend 429
        {"error": {"message": "Too many requests. Please try again later.",
                   "type": "invalid_request_error", "code": "rate_limit_exceeded"}}
    Existing account login failed: existing_login_otp_send_failed:429
    [!] Registration failed for ae***@icloud.com: existing_login_otp_send_failed:429
    ...
    Account 2/3          <-- next account attempted immediately
    ...
    [!] Registration failed for gr***@icloud.com: existing_login_otp_send_failed:429

Three addresses in one run, each preceded by a full signup attempt, with no pause
between them.  The throttle is per-exit, not per-address, so "the next account"
is not more likely to succeed.

What this file pins down: the circuit is armed with the server's own
``Retry-After``, the reported string is the one the signup lane already uses (so
it classifies ``rate_limit`` and stays a hard stop), the circuit is actually
*consumed* by the next stage admission, and a non-429 still reports exactly what
it used to.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import pytest

from sms_tool import auth_flow, registration_concurrency
from sms_tool.error_classification import classify_error
from sms_tool.registration_policy import registration_retry_decision


CHAT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"
USERNAME = "someone@icloud.com"

# The landing the live run reached, and the reason the continue POST is skipped.
EMAIL_VERIFICATION_LANDING = f"{AUTH_BASE}/email-verification"

SIGNUP_LANE_PREFIX = "registration_rate_limited:retry_after="
LEGACY_PREFIX = "existing_login_otp_send_failed:"


@pytest.fixture(autouse=True)
def _clean_rate_limit_circuit():
    """``mark_registration_rate_limited`` is process-global.

    Without this, one armed test parks every later registration test in the
    session -- the classic green-alone / red-in-full-run shape.
    """
    registration_concurrency.clear_registration_rate_limit()
    yield
    registration_concurrency.clear_registration_rate_limit()


class _Response:
    """Minimal stand-in for a curl_cffi response."""

    def __init__(self, *, status_code=200, url="", body=None, headers=None):
        self.status_code = status_code
        self.url = url
        self._body = {} if body is None else body
        self.headers = {} if headers is None else headers

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


class _Recorder:
    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def __call__(self, session, method, url, **kwargs):
        record = {"session": session, "method": method, "url": url, "kwargs": kwargs}
        self.calls.append(record)
        return self._handler(record)

    def urls(self):
        return [record["url"] for record in self.calls]


def _drive(*, otp_status, otp_headers=None, otp_body=None):
    """Run the real function against a scripted transport.

    The scripted flow lands on ``/email-verification``, which is the state the
    live run was in (``Existing account continue: skipped``), and answers the
    OTP send with ``otp_status``.
    """
    def handler(record):
        url = record["url"]
        if url == f"{CHAT_BASE}/":
            return _Response(body={})
        if url == f"{CHAT_BASE}/api/auth/csrf":
            return _Response(body={"csrfToken": "session-token"})
        if url.startswith(f"{CHAT_BASE}/api/auth/signin/openai"):
            return _Response(body={"url": f"{AUTH_BASE}/api/accounts/authorize"})
        if url.startswith(f"{AUTH_BASE}/api/accounts/authorize"):
            return _Response(url=EMAIL_VERIFICATION_LANDING)
        if "email-otp" in url:
            return _Response(
                status_code=otp_status,
                headers={} if otp_headers is None else otp_headers,
                body={} if otp_body is None else otp_body,
            )
        raise AssertionError(f"unexpected call: {record['method']} {url}")

    recorder = _Recorder(handler)
    with patch.object(auth_flow, "request_with_retry", recorder), patch.object(
        auth_flow, "_authorize_continue_sentinel", lambda *a, **k: ({}, "", "")
    ):
        result = auth_flow._login_existing_account_with_email_otp(
            session=object(),
            username=USERNAME,
            mailbox=None,
            did="did-1",
            session_logging_id="slog-1",
            auth_base=AUTH_BASE,
            chat_base=CHAT_BASE,
            base_headers={},
            csrf_token="caller-token",
            proxy="",
        )
    return result, recorder


# --------------------------------------------------------------------------
# the 429
# --------------------------------------------------------------------------

def test_a_429_arms_the_registration_rate_limit_circuit():
    result, _ = _drive(
        otp_status=429,
        otp_body={"error": {"message": "Too many requests", "code": "rate_limit_exceeded"}},
    )

    assert result["ok"] is False
    assert registration_concurrency.registration_rate_limit_remaining() > 0


def test_a_429_reports_the_string_the_signup_lane_already_uses():
    result, _ = _drive(otp_status=429)

    assert result["error"].startswith(SIGNUP_LANE_PREFIX)


def test_the_servers_retry_after_is_used_not_a_local_guess():
    result, _ = _drive(otp_status=429, otp_headers={"Retry-After": "42"})

    assert result["error"] == f"{SIGNUP_LANE_PREFIX}42s"
    # The circuit must agree with the string the operator was shown.
    assert 41 < registration_concurrency.registration_rate_limit_remaining() <= 42


def test_a_missing_retry_after_falls_back_to_the_shared_default():
    result, _ = _drive(otp_status=429, otp_headers={})

    assert result["error"] == f"{SIGNUP_LANE_PREFIX}300s"


def test_the_429_stays_a_hard_stop():
    """The class changes; the "do not retry this address" verdict must not."""
    result, _ = _drive(otp_status=429)

    assert classify_error(result["error"]) == "rate_limit"
    assert registration_retry_decision(result["error"]).retryable is False


def test_the_armed_circuit_actually_blocks_the_next_stage_admission():
    """Armed is not the same as consumed -- assert on the real consumer."""
    _drive(otp_status=429, otp_headers={"Retry-After": "30"})

    with pytest.raises(RuntimeError) as caught:
        registration_concurrency.acquire_registration_stage("auth_flow")

    assert "registration_rate_limit_circuit_open" in str(caught.value)
    assert "retry_after=" in str(caught.value)


# --------------------------------------------------------------------------
# the control: everything that is not a 429 keeps its old behaviour
# --------------------------------------------------------------------------

def test_a_non_429_keeps_the_old_error_and_leaves_the_circuit_closed():
    result, _ = _drive(otp_status=500)

    assert result["error"] == f"{LEGACY_PREFIX}500"
    assert registration_concurrency.registration_rate_limit_remaining() == 0


def test_a_400_keeps_the_old_error_and_leaves_the_circuit_closed():
    """400 is one of the statuses ``_send_existing_login_otp`` falls through on,
    so it is the most likely neighbour of the 429 branch."""
    result, _ = _drive(otp_status=400)

    assert result["error"] == f"{LEGACY_PREFIX}400"
    assert registration_concurrency.registration_rate_limit_remaining() == 0


if __name__ == "__main__":
    unittest.main()
