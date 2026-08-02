from unittest.mock import patch

from sms_tool import account_recovery


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


def test_relogin_auto_uses_oauth_without_web_fallback():
    with (
        patch.object(account_recovery, "relogin_web_session_account") as web,
        patch.object(
            account_recovery,
            "relogin_local_codex_account",
            return_value={"ok": True, "mode": "codex_oauth_pkce"},
        ) as oauth,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["ok"]
    web.assert_not_called()
    oauth.assert_called_once()


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
