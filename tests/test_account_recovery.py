from unittest.mock import patch

from sms_tool import account_recovery


def test_chatgpt_email_relogin_validates_account_input():
    invalid = account_recovery.relogin_chatgpt_email_account(None)
    missing_email = account_recovery.relogin_chatgpt_email_account({})

    assert invalid == {"ok": False, "mode": "chatgpt_email_otp", "error": "invalid_account"}
    assert missing_email == {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_email"}


def test_chatgpt_email_relogin_requires_saved_mailbox():
    with patch("sms_tool.codex_oauth._mailbox_from_data", return_value=None):
        result = account_recovery.relogin_chatgpt_email_account({"email": "ok@example.com"})

    assert result == {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_mailbox"}


def test_refresh_local_quota_statuses_persists_result():
    with (
        patch.object(account_recovery, "get_account_record", return_value={"email": "ok@example.com", "access_token": "at_123"}),
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "quota_status": "active"}),
        patch.object(account_recovery, "mark_quota_status", return_value=True) as marked,
    ):
        result = account_recovery.refresh_local_quota_statuses(["ok@example.com"])

    assert result["ok"]
    marked.assert_called_once()
    assert marked.call_args.args[:2] == ("ok@example.com", "active")


def test_refresh_local_quota_statuses_recovers_401():
    with (
        patch.object(account_recovery, "get_account_record", return_value={"email": "ok@example.com", "access_token": "old_at"}),
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": False, "status": "token_invalid", "quota_status": "invalid"}),
        patch.object(
            account_recovery,
            "relogin_codex_account",
            return_value={"ok": True, "probe": {"ok": True, "status": "active", "status_code": 200, "quota_status": "active"}},
        ) as relogin,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["ok@example.com"],
            relogin_on_401=True,
            relogin_mode="codex_oauth",
        )

    assert result["ok"]
    assert result["results"][0]["quota_status"] == "active"
    assert relogin.call_args.kwargs["mode"] == "codex_oauth"


def test_relogin_auto_uses_refresh_cookie_browser_then_oauth():
    with (
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "mode": "oauth_refresh_token", "error": "invalid_grant"},
        ) as refresh,
        patch.object(
            account_recovery,
            "relogin_web_session_account",
            return_value={"ok": False, "mode": "web_session", "error": "missing_session_cookie"},
        ) as web,
        patch.object(
            account_recovery,
            "relogin_browser_account",
            return_value={"ok": False, "mode": "browser", "error": "browser_login_challenge_required"},
        ) as browser,
        patch.object(
            account_recovery,
            "relogin_local_codex_account",
            return_value={"ok": True, "mode": "codex_oauth_pkce"},
        ) as oauth,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["ok"]
    assert [item["mode"] for item in result["attempts"]] == [
        "oauth_refresh_token",
        "web_session",
        "browser",
    ]
    refresh.assert_called_once()
    web.assert_called_once()
    browser.assert_called_once()
    oauth.assert_called_once()


def test_relogin_auto_stops_after_refresh_token_success():
    with (
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": True, "mode": "oauth_refresh_token", "persisted": True},
        ) as refresh,
        patch.object(account_recovery, "relogin_web_session_account") as web,
        patch.object(account_recovery, "relogin_browser_account") as browser,
        patch.object(account_recovery, "relogin_local_codex_account") as oauth,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["ok"]
    assert result["mode"] == "oauth_refresh_token"
    assert result["attempts"] == []
    refresh.assert_called_once()
    web.assert_not_called()
    browser.assert_not_called()
    oauth.assert_not_called()


def test_relogin_persists_only_after_http_200_probe():
    oauth_result = {"ok": True, "tokens": {"access_token": "new_at", "refresh_token": "rt_new"}}
    with (
        patch("sms_tool.codex_oauth.refresh_codex_oauth_session", return_value=oauth_result),
        patch("sms_tool.codex_oauth._save_oauth_tokens", return_value={"ok": True, "mode": "codex_oauth_pkce"}) as save,
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "status": "active", "status_code": 200}),
    ):
        result = account_recovery.relogin_local_codex_account({"email": "ok@example.com", "access_token": "old_at"})

    assert result["ok"]
    assert result["persisted"]
    save.assert_called_once()


def test_refresh_token_recovery_verifies_before_persisting():
    account = {
        "email": "ok@example.com",
        "access_token": "old_at",
        "oauth_refresh_token": "rt_old",
        "json_path": "session.json",
    }
    with (
        patch("sms_tool.codex_export._openai_refresh_token", return_value="rt_old"),
        patch("sms_tool.codex_export._refresh_with_openai_oauth", return_value={
            "ok": True,
            "data": {"access_token": "new_at", "oauth_refresh_token": "rt_new"},
        }),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": True, "status": "active", "status_code": 200},
        ) as probe,
        patch("sms_tool.session_refresh._save_refreshed", return_value="session.json") as save,
    ):
        result = account_recovery.relogin_refresh_token_account(account)

    assert result["ok"]
    assert result["mode"] == "oauth_refresh_token"
    assert result["persisted"]
    assert probe.call_args.args[0]["access_token"] == "new_at"
    assert save.call_args.args[0]["oauth_refresh_token"] == "rt_new"


def test_refresh_token_recovery_rejects_unverified_candidate():
    account = {"email": "ok@example.com", "oauth_refresh_token": "rt_old"}
    with (
        patch("sms_tool.codex_export._openai_refresh_token", return_value="rt_old"),
        patch("sms_tool.codex_export._refresh_with_openai_oauth", return_value={
            "ok": True,
            "data": {"access_token": "new_at"},
        }),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "status_code": 401},
        ),
        patch("sms_tool.session_refresh._save_refreshed") as save,
    ):
        result = account_recovery.relogin_refresh_token_account(account)

    assert not result["ok"]
    assert result["error"] == "oauth_refresh_token_access_token_probe_failed:401"
    save.assert_not_called()


def test_web_session_rejects_a_cookie_for_another_account():
    candidate = {
        "email": "ok@example.com",
        "access_token": "new_at",
        "auth_session": {"user": {"email": "other@example.com"}},
    }
    with (
        patch("sms_tool.session_refresh._refresh_session_protocol", return_value={"ok": True, "data": candidate}),
        patch.object(account_recovery, "probe_account_liveness") as probe,
        patch("sms_tool.session_refresh._save_refreshed") as save,
    ):
        result = account_recovery.relogin_web_session_account({"email": "ok@example.com"})

    assert not result["ok"]
    assert result["error"] == "auth_session_email_mismatch"
    probe.assert_not_called()
    save.assert_not_called()


def test_recovery_proxy_uses_registration_country_and_pool():
    with (
        patch.dict(account_recovery.CFG, {
            "proxy": {
                "pool": ["http://pool.example:8080"],
                "registration": "http://registration.example:8080",
                "default": "http://default.example:8080",
            }
        }, clear=False),
        patch(
            "sms_tool.paypal_proxy.select_proxy_from_pool",
            return_value=("http://selected.example:8080", [{"ok": True, "expected_country": "JP"}]),
        ) as select,
    ):
        proxy, attempts = account_recovery._select_recovery_proxy(
            {"registration_country": "jp"},
            "http://explicit.example:8080",
        )

    assert proxy == "http://selected.example:8080"
    assert attempts[0]["ok"]
    assert select.call_args.args[1:] == ("JP", "account_recovery")
    assert select.call_args.args[0][0] == "http://explicit.example:8080"
