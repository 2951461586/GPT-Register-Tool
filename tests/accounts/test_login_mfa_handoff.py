"""The password login must actually finish the MFA challenge it walks into.

Measured 2026-11-05 on ``setback-changer8w+oai02`` (a dead iCloud account whose
session cookie could no longer mint a token):

    password verify      -> 200
    password continue    -> 200  https://auth.openai.com/mfa-challenge/<id>
    password landing     -> 200  https://chatgpt.com/
    auth session         -> 200  (no access token)

The login *did* reach the challenge, and the TOTP step silently skipped it, so
the lane ended with ``auth_session_missing_access_token`` while everything else
looked successful. Two gaps made that happen:

1. the followed ``/mfa-challenge/`` body is an HTML shell, so the payload kept
   the pre-challenge dict and ``_is_mfa_challenge_payload`` answered False;
2. the transaction carried no ``oai-client-auth-session`` factor array, so even
   a correct challenge payload had no factor id -- although the id is right
   there in the URL this module already uses as its referer default.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from sms_tool.auth_flow import password_step, totp

CHALLENGE_URL = "https://auth.openai.com/mfa-challenge/6ab0b8fcef208191aa70e29830dfbdbb"
FACTOR_ID = "6ab0b8fcef208191aa70e29830dfbdbb"


# ── 1. the factor id can come from the challenge URL ───────────────────────
def test_factor_id_falls_back_to_the_challenge_url():
    payload = {"continue_url": CHALLENGE_URL}
    assert totp._is_mfa_challenge_payload(payload) is True
    assert totp._totp_factor_id(payload) == FACTOR_ID


def test_factor_id_prefers_the_declared_factor_array():
    payload = {
        "oai-client-auth-session": {
            "mfa_challenge_factors": [{"id": "declared-id", "factor_type": "totp"}],
        },
        "continue_url": CHALLENGE_URL,
    }
    assert totp._totp_factor_id(payload) == "declared-id"


def test_no_challenge_anywhere_means_no_factor():
    assert totp._totp_factor_id({}) == ""
    assert totp._totp_factor_id({"continue_url": "https://auth.openai.com/log-in/password"}) == ""


# ── 2. the challenge URL survives an HTML "continue" body ──────────────────
def _response(status: int = 200, payload=None):
    response = Mock()
    response.status_code = status
    response.headers = {}
    response.text = ""
    response._payload = payload or {}
    return response


def test_the_totp_step_receives_the_challenge_url_from_an_html_continue():
    verify_response = _response()
    followed_response = _response()
    seen: dict = {}

    def fake_json_or_raw(response, limit=0):
        if response is verify_response:
            return {"continue_url": CHALLENGE_URL}
        # The followed body is an HTML shell: not a dict, so the old code kept
        # the pre-challenge payload and the TOTP step never ran.
        return "<!doctype html><html></html>"

    def fake_complete(session, auth_base, base_headers, payload, **kwargs):
        seen["payload"] = payload
        seen["totp_secret"] = kwargs.get("totp_secret")
        return {"ok": True, "data": payload}

    with (
        patch.object(password_step.deps, "request_with_retry", return_value=verify_response),
        patch.object(password_step.deps, "_json_or_raw", side_effect=fake_json_or_raw),
        patch.object(password_step.steps, "_response_next_url", return_value=CHALLENGE_URL),
        patch.object(password_step.deps, "_follow_continue_url", return_value=followed_response),
        patch.object(password_step.totp, "_complete_existing_login_totp", side_effect=fake_complete),
    ):
        result = password_step._password_login_existing_account(
            Mock(),
            "https://auth.openai.com",
            {},
            "correct-horse",
            "https://auth.openai.com/log-in/password",
            "did-1",
            totp_secret="JBSWY3DPEHPK3PXP",
        )

    assert result.get("ok") is True
    assert seen["payload"]["continue_url"] == CHALLENGE_URL
    assert totp._is_mfa_challenge_payload(seen["payload"]) is True
    assert totp._totp_factor_id(seen["payload"]) == FACTOR_ID
    assert seen["totp_secret"] == "JBSWY3DPEHPK3PXP"
