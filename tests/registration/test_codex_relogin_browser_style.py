"""Relogin: browser-style ``passwordless/send-otp``, password-first, and TOTP.

Grounded in the 2026-10-11 browser HAR (empty-body send with a fresh
``authorize_continue`` Sentinel) and the 10-11 relogin log, where a working
password step was overridden into an email-OTP lane whose code never arrived.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from sms_tool import codex_oauth

SEND = "https://auth.openai.com/api/accounts/passwordless/send-otp"


def _ok_response(pending=False, mode="passwordless_signup"):
    return SimpleNamespace(
        status_code=200,
        text="{}",
        json=lambda: {
            "oai-client-auth-session": {
                "passwordless_email_otp_send_pending": pending,
                "email_verification_mode": mode,
            }
        },
    )


def _cfg(**flags):
    return patch.object(codex_oauth, "CFG", {"codex_oauth": flags})


def test_browser_style_send_is_the_default_when_unset():
    with _cfg():
        assert codex_oauth._browser_style_send_otp_enabled() is True
    with _cfg(browser_style_send_otp=False):
        assert codex_oauth._browser_style_send_otp_enabled() is False


def test_legacy_send_keeps_the_cached_sentinel_and_json_body_but_logs_the_answer(capsys):
    session = Mock()
    session.post.return_value = _ok_response(pending=True, mode="weird-mode-string")
    with (
        _cfg(browser_style_send_otp=False),
        patch.object(codex_oauth, "load_cached_sentinel", return_value={"sentinel_token": "cached"}),
    ):
        result = codex_oauth._send_passwordless_otp(session, "did-1", "https://auth.openai.com/log-in")
    assert result["ok"]
    kwargs = session.post.call_args.kwargs
    assert kwargs["json"] == {}
    assert kwargs["headers"]["openai-sentinel-token"] == "cached"
    out = capsys.readouterr().out
    assert "pending=true mode=other" in out
    assert "weird-mode-string" not in out
    assert "browser-style" not in out


def test_browser_style_send_mints_a_fresh_flow_token_and_posts_an_empty_body(capsys):
    session = Mock()
    session.post.return_value = _ok_response()
    issued = SimpleNamespace(token="fresh-json", so_token="so")
    with (
        _cfg(browser_style_send_otp=True),
        patch.object(codex_oauth, "load_cached_sentinel", return_value={"sentinel_token": "cached"}),
        patch.object(codex_oauth, "issue_sentinel_flow", return_value=issued) as mint,
    ):
        result = codex_oauth._send_passwordless_otp(session, "did-1", "https://auth.openai.com/log-in", proxy="p")
    assert result["ok"]
    assert mint.call_args.kwargs["flow"] == "authorize_continue"
    assert mint.call_args.kwargs["device_id"] == "did-1"
    assert mint.call_args.kwargs["proxy"] == "p"
    assert session.post.call_args.args[0] == SEND
    kwargs = session.post.call_args.kwargs
    assert "json" not in kwargs and "data" not in kwargs
    headers = {k.lower(): v for k, v in kwargs["headers"].items()}
    assert headers["openai-sentinel-token"] == "fresh-json"
    assert "openai-sentinel-so-token" not in headers
    assert headers["referer"] == "https://auth.openai.com/email-verification"
    assert headers["x-openai-document-navigation-id"] and headers["x-access-flow-invocation-id"]
    # None (not absence) is what makes curl_cffi drop libcurl's default
    # form-urlencoded label, which the server rejects as unsupported_content_type.
    assert "content-type" in headers and headers["content-type"] is None
    out = capsys.readouterr().out
    assert "Passwordless browser-style send-otp" in out
    assert "pending=false mode=passwordless_signup" in out


def test_browser_style_send_fails_the_arm_when_the_sentinel_cannot_be_minted():
    session = Mock()
    with (
        _cfg(browser_style_send_otp=True),
        patch.object(codex_oauth, "issue_sentinel_flow", side_effect=RuntimeError("runner down")),
    ):
        result = codex_oauth._send_passwordless_otp(session, "did-1", "u")
    assert result["hard_error"]
    assert result["error"].startswith("passwordless_send_sentinel_failed:")
    session.post.assert_not_called()


def _stage(force=True, stage_url="https://auth.openai.com/log-in/password", data=None):
    return codex_oauth._run_protocol_login_stages(
        session=Mock(),
        oauth={},
        email="user@example.com",
        data=data if data is not None else {"password": "pw"},
        did="did-1",
        current_url=stage_url,
        force_email_otp_login=force,
    )


def test_password_first_is_off_by_default_and_the_forced_lane_is_unchanged():
    with (
        _cfg(),
        patch.object(codex_oauth, "_password_login_and_exchange") as password,
        patch.object(codex_oauth, "_passwordless_login_and_exchange", return_value={"ok": False}) as otp,
    ):
        _stage()
    password.assert_not_called()
    otp.assert_called_once()


def test_password_first_returns_without_spending_a_code_when_the_password_path_works(capsys):
    with (
        _cfg(password_first_relogin=True),
        patch.object(codex_oauth, "_password_login_and_exchange", return_value={"ok": True, "tokens": {}}) as password,
        patch.object(codex_oauth, "_passwordless_login_and_exchange") as otp,
    ):
        result = _stage()
    assert result["ok"] and result["protocol_stage"] == "password"
    password.assert_called_once()
    otp.assert_not_called()
    assert "Relogin password-first" in capsys.readouterr().out


def test_password_first_falls_back_to_email_otp_when_the_password_path_fails():
    with (
        _cfg(password_first_relogin=True),
        patch.object(codex_oauth, "_password_login_and_exchange", return_value={"ok": False, "error": "x"}),
        patch.object(codex_oauth, "_passwordless_login_and_exchange", return_value={"ok": False}) as otp,
    ):
        _stage()
    otp.assert_called_once()


def test_password_first_needs_a_saved_password():
    with (
        _cfg(password_first_relogin=True),
        patch.object(codex_oauth, "_password_login_and_exchange") as password,
        patch.object(codex_oauth, "_passwordless_login_and_exchange", return_value={"ok": False}),
    ):
        _stage(data={})
    password.assert_not_called()


def _mfa_verify_response():
    body = {
        "continue_url": "https://auth.openai.com/mfa-challenge/f1",
        "page": {"type": "mfa_challenge"},
        "oai-client-auth-session": {"mfa_factors": [{"factor_type": "totp", "id": "f1"}]},
    }
    return SimpleNamespace(
        status_code=200,
        text="{}",
        url="https://auth.openai.com/api/accounts/password/verify",
        headers={},
        json=lambda: body,
    )


def test_password_login_completes_a_saved_totp_challenge_instead_of_failing_on_the_mfa_page():
    session = Mock()
    session.post.return_value = _mfa_verify_response()
    followed = []

    def follow(_session, url, proxy=None):
        followed.append(url)
        return None, "https://chatgpt.com/callback?code=abc"

    with (
        patch.object(codex_oauth, "load_cached_sentinel", return_value={}),
        patch("sms_tool.auth_flow.totp._complete_existing_login_totp") as totp,
        patch.object(codex_oauth, "_follow_redirects", side_effect=follow),
        patch.object(codex_oauth, "_has_callback_code", return_value=True),
        patch.object(codex_oauth, "_exchange_callback", return_value={"access_token": "at"}),
    ):
        totp.return_value = {
            "ok": True,
            "data": {"continue_url": "https://auth.openai.com/api/accounts/authorize/resume"},
        }
        result = codex_oauth._password_login_and_exchange(
            session, {}, {"password": "pw", "totp_secret": "SECRET"}, "did-1", "https://auth.openai.com/log-in/password"
        )
    assert result["ok"] and result["login_method"] == "password"
    assert totp.call_args.kwargs["totp_secret"] == "SECRET"
    assert followed == ["https://auth.openai.com/api/accounts/authorize/resume"]


def test_password_login_reports_a_missing_totp_secret_as_its_own_error():
    session = Mock()
    session.post.return_value = _mfa_verify_response()
    with patch.object(codex_oauth, "load_cached_sentinel", return_value={}):
        result = codex_oauth._password_login_and_exchange(
            session, {}, {"password": "pw"}, "did-1", "https://auth.openai.com/log-in/password"
        )
    assert not result["ok"]
    assert result["error"] == "password_login_mfa_failed:existing_login_totp_secret_missing"


def test_config_rejects_non_boolean_relogin_toggles():
    import pytest

    from sms_tool.config import ConfigError, validate_config

    for key in ("browser_style_send_otp", "password_first_relogin"):
        with pytest.raises(ConfigError, match=rf"codex_oauth\.{key} must be a boolean"):
            validate_config({"codex_oauth": {key: "yes"}})
