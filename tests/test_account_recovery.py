from unittest.mock import patch

from sms_tool import account_recovery
from sms_tool import codex_oauth
from sms_tool.mailbox import MailboxAccount
from sms_tool.account_identity import create_registration_identity


def test_chatgpt_email_relogin_validates_account_input():
    invalid = account_recovery.relogin_chatgpt_email_account(None)
    missing_email = account_recovery.relogin_chatgpt_email_account({})

    assert invalid == {"ok": False, "mode": "chatgpt_email_otp", "error": "invalid_account"}
    assert missing_email == {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_email"}


def test_chatgpt_email_relogin_requires_saved_mailbox():
    with patch("sms_tool.codex_oauth._mailbox_from_data", return_value=None):
        result = account_recovery.relogin_chatgpt_email_account({"email": "ok@example.com"})

    assert result == {"ok": False, "mode": "chatgpt_email_otp", "error": "missing_mailbox"}


def test_icloud_mailbox_url_is_resolved_from_configured_pool_without_persisting_it():
    mailbox = MailboxAccount(
        email="ok@icloud.com",
        provider="icloud_url",
        source="token_file",
        token="https://mail.example/private-token/ok@icloud.com",
        auth_mode="otp_url",
    )
    with patch.object(codex_oauth, "_mailbox_from_configured_pool", return_value=mailbox) as lookup:
        result = codex_oauth._mailbox_from_data({
            "email": "ok@icloud.com",
            "mailbox": {"email": "ok@icloud.com", "provider": "icloud_url", "source": "token_file"},
        })

    assert result is mailbox
    lookup.assert_called_once_with("ok@icloud.com")


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


def test_refresh_local_quota_statuses_emits_terminal_event_per_account(monkeypatch):
    events = []
    monkeypatch.setenv("SMSWORKBENCH_EVENTS", "1")
    monkeypatch.setattr("sms_tool.desktop_ipc.emit_event", lambda payload, enabled=None: events.append(payload) or True)
    monkeypatch.setattr(account_recovery, "_local_quota_accounts", lambda emails: [
        {"email": "a@example.com", "access_token": "at-a"},
        {"email": "b@example.com", "access_token": "at-b"},
    ])
    monkeypatch.setattr(account_recovery, "probe_account_liveness", lambda account, **kwargs: {"ok": True, "quota_status": "active"})
    monkeypatch.setattr(account_recovery, "mark_quota_status", lambda *args, **kwargs: True)

    result = account_recovery.refresh_local_quota_statuses(["a@example.com", "b@example.com"], workers=2)

    terminal = [event for event in events if event.get("stage") == "account_completed"]
    assert result["total"] == 2
    assert len(terminal) == 2
    assert {event["account_ref"] for event in terminal} == {"a@example.com", "b@example.com"}
    assert all(event["total"] == 2 for event in terminal)


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
    assert result["relogin_attempted"] == 1
    assert result["relogin_success"] == 1
    assert result["relogin_failed"] == 0
    assert relogin.call_args.kwargs["mode"] == "codex_oauth"


def test_refresh_local_quota_statuses_does_not_count_persisted_401_as_success():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "invalid@example.com", "access_token": "expired_at"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={
                "ok": False,
                "status": "token_invalid",
                "status_code": 401,
                "quota_status": "401失效",
            },
        ),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["invalid@example.com"])

    assert not result["ok"]
    assert result["success"] == 0
    assert result["failed"] == 1
    assert result["persisted"] == 1
    assert result["persist_failed"] == 0
    assert result["at_invalid"] == 1
    assert result["account_deactivated"] == 0
    assert result["probe_failed"] == 0
    assert result["results"][0]["persisted"] is True
    assert result["results"][0]["probe_ok"] is False
    assert result["results"][0]["ok"] is False


def test_refresh_local_quota_statuses_accepts_http_401_without_normalized_status():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "status-code-only@example.com", "access_token": "expired_at"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status_code": 401, "quota_status": "401失效"},
        ),
        patch.object(
            account_recovery,
            "relogin_codex_account",
            return_value={
                "ok": True,
                "probe": {"ok": True, "status_code": 200, "status": "active"},
            },
        ) as relogin,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["status-code-only@example.com"],
            relogin_on_401=True,
        )

    assert result["ok"]
    relogin.assert_called_once()


def test_refresh_local_quota_statuses_classifies_terminal_account_without_relogin():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={
                "email": "closed@example.com",
                "access_token": "expired_at",
                "status": "account_deactivated",
            },
        ),
        patch.object(account_recovery, "probe_account_liveness") as probe,
        patch.object(account_recovery, "relogin_codex_account") as relogin,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["closed@example.com"],
            relogin_on_401=True,
        )

    assert not result["ok"]
    assert result["account_deactivated"] == 1
    assert result["at_invalid"] == 0
    assert result["probe_failed"] == 0
    assert result["relogin_attempted"] == 0
    probe.assert_not_called()
    relogin.assert_not_called()


def test_relogin_auto_uses_refresh_cookie_email_then_oauth():
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
            "relogin_chatgpt_email_account",
            return_value={"ok": False, "mode": "chatgpt_email_otp", "error": "email_login_failed"},
        ) as email_otp,
        patch.object(
            account_recovery,
            "relogin_local_codex_account",
            return_value={"ok": True, "mode": "codex_oauth_pkce"},
        ) as oauth,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["ok"]
    # Browser re-login has been removed: the auto chain is protocol-only.
    assert [item["mode"] for item in result["attempts"]] == [
        "oauth_refresh_token",
        "web_session",
        "chatgpt_email_otp",
    ]
    refresh.assert_called_once()
    web.assert_called_once()
    email_otp.assert_called_once()
    oauth.assert_called_once()
    assert not hasattr(account_recovery, "relogin_browser_account")


def test_relogin_reuses_account_proxy_affinity_for_every_strategy():
    base_proxy = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"
    registration_proxy = "http://user-region-US-sid-NEW5678-t-5:secret@proxy.example:443"
    account = {
        "email": "ok@example.com",
        "identity_context": create_registration_identity(
            registration_proxy,
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
        ),
    }
    config = {"proxy": {"registration": base_proxy, "pool": [base_proxy]}}

    with (
        patch.object(account_recovery, "CFG", config),
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "mode": "oauth_refresh_token", "error": "invalid_grant"},
        ) as refresh,
        patch.object(
            account_recovery,
            "relogin_web_session_account",
            return_value={"ok": True, "mode": "web_session"},
        ) as web,
    ):
        result = account_recovery.relogin_codex_account(
            account,
            proxy="http://127.0.0.1:7897",
            mode="auto",
        )

    assert result["ok"]
    assert refresh.call_args.kwargs["proxy"] == registration_proxy
    assert web.call_args.kwargs["proxy"] == registration_proxy


def test_relogin_auto_stops_after_refresh_token_success():
    with (
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": True, "mode": "oauth_refresh_token", "persisted": True},
        ) as refresh,
        patch.object(account_recovery, "relogin_web_session_account") as web,
        patch.object(account_recovery, "relogin_chatgpt_email_account") as email_otp,
        patch.object(account_recovery, "relogin_local_codex_account") as oauth,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["ok"]
    assert result["mode"] == "oauth_refresh_token"
    assert result["attempts"] == []
    refresh.assert_called_once()
    web.assert_not_called()
    email_otp.assert_not_called()
    oauth.assert_not_called()


def test_relogin_auto_persists_permanent_deactivation():
    with (
        patch.object(
            account_recovery,
            "relogin_refresh_token_account",
            return_value={"ok": False, "mode": "oauth_refresh_token", "error": "account_deactivated"},
        ),
        patch.object(account_recovery, "_persist_permanent_deactivation", return_value=True) as persist,
        patch.object(account_recovery, "relogin_web_session_account") as web,
    ):
        result = account_recovery.relogin_codex_account({"email": "ok@example.com"}, mode="auto")

    assert result["terminal"] is True
    assert result["error"] == "account_deactivated"
    persist.assert_called_once()
    web.assert_not_called()


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


def test_successful_relogin_replaces_stale_quota_401_metadata():
    data = {
        "status": "at_invalid",
        "error": "oauth_refresh_http_401",
        "quota_status": "401失效",
        "quota": {
            "status": "401失效",
            "last_result": {"status": "token_invalid", "status_code": 401},
        },
    }
    probe = {
        "ok": True,
        "status": "active",
        "status_code": 200,
        "quota_status": "可用",
        "access_token": "must-not-persist-in-quota-metadata",
    }

    account_recovery._mark_successful_relogin(data, probe, now=123)

    assert data["status"] == "registered"
    assert "error" not in data
    assert data["quota_status"] == "可用"
    assert data["quota_updated_at"] == 123
    assert data["quota"]["status"] == "可用"
    assert data["quota"]["updated_at"] == 123
    assert data["quota"]["last_result"]["status_code"] == 200
    assert "access_token" not in data["quota"]["last_result"]


def test_successful_relogin_clears_stale_promotion_at_marker():
    data = {
        "status": "at_invalid",
        "promotion_status": "AT失效",
        "promotion": {"status": "AT失效", "last_result": {"status_code": 401}},
    }
    probe = {
        "ok": True,
        "status": "active",
        "status_code": 200,
        "quota_status": "可用",
    }

    account_recovery._mark_successful_relogin(data, probe, now=123)

    assert data["promotion_status"] == ""
    assert data["promotion"]["status"] == ""
    assert data["promotion"]["last_result"]["status_code"] == 401


def test_refresh_token_recovery_verifies_before_persisting():
    account = {
        "email": "ok@example.com",
        "access_token": "old_at",
        "oauth_refresh_token": "rt_old",
        "json_path": "session.json",
        "success": False,
        "status": "at_invalid",
        "error": "oauth_refresh_http_401",
        "account_scan": {"token_probe": {"status": "token_invalid", "status_code": 401}},
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
    assert save.call_args.args[0]["status"] == "registered"
    assert "error" not in save.call_args.args[0]
    assert save.call_args.args[0]["account_scan_status"] == "alive"
    assert save.call_args.args[0]["account_scan"]["token_probe"]["status_code"] == 200


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



def test_refresh_local_quota_statuses_clears_stale_promotion_marker_after_relogin():
    cleared = {"called": False}

    def fake_clear(email):
        cleared["called"] = True
        return True

    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "stale@example.com", "access_token": "old_at"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": False, "status": "token_invalid", "quota_status": "401SHIXIAO"},
        ),
        patch.object(
            account_recovery,
            "relogin_codex_account",
            return_value={"ok": True, "probe": {"ok": True, "status": "active", "status_code": 200, "quota_status": "active"}},
        ),
        patch.object(account_recovery, "clear_stale_promotion_at_marker", side_effect=fake_clear),
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(
            ["stale@example.com"],
            relogin_on_401=True,
            relogin_mode="codex_oauth",
        )

    assert result["relogin_success"] == 1
    assert cleared["called"]


def test_refresh_local_quota_statuses_skips_promotion_clear_without_relogin():
    with (
        patch.object(
            account_recovery,
            "get_account_record",
            return_value={"email": "ok@example.com", "access_token": "at_123"},
        ),
        patch.object(
            account_recovery,
            "probe_account_liveness",
            return_value={"ok": True, "quota_status": "active"},
        ),
        patch.object(account_recovery, "clear_stale_promotion_at_marker") as clear_marker,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["ok@example.com"])

    assert result["ok"]
    clear_marker.assert_not_called()


def test_desktop_read_hides_stale_promotion_at_marker_after_verified_200():
    import json as json_mod
    from sms_tool.desktop_read import _record_payload

    stale_label = "AT" + chr(0x5931) + chr(0x6548)

    def record_with(probe_state):
        return {
            "id": "1",
            "email": "stale@example.com",
            "json_path": "",
            "raw_json": json_mod.dumps({
                "email": "stale@example.com",
                "promotion_status": stale_label,
                "promotion": {"status": stale_label, "last_result": {"status_code": 401}},
                **probe_state,
            }),
        }

    fresh_probe = {
        "quota": {"last_result": {"status_code": 200}, "status": "ok"},
        "quota_updated_at": 200,
        "account_scan": {"token_probe": {"status_code": 401}},
        "account_scan_updated_at": 100,
    }
    payload = _record_payload(record_with(fresh_probe))
    assert "promotion_status" not in payload
    assert payload["at_probe_status_code"] == "200"

    still_401 = {
        "quota": {"last_result": {"status_code": 401}, "status": "bad"},
        "quota_updated_at": 200,
    }
    payload_401 = _record_payload(record_with(still_401))
    assert payload_401["promotion_status"] == stale_label


def test_browser_recovery_uses_driver_from_browser_identity():
    """Browser recovery reopens the same driver recorded at registration."""
    from unittest.mock import MagicMock, patch

    account = {
        "email": "browser@example.com",
        "access_token": "expired_at",
        "identity_context": create_registration_identity(
            "http://proxy.example:8080",
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
            account_key="browser@example.com",
            browser_identity={"driver": "cloak", "profile_id": "browser@example.com"},
        ),
    }
    mock_browser = MagicMock()
    mock_browser.__enter__ = MagicMock(return_value=mock_browser)
    mock_browser.__exit__ = MagicMock(return_value=False)
    mock_browser.page = MagicMock()
    mock_browser.cookie_header.return_value = ""

    with (
        patch("sms_tool.account_recovery.CFG", {"chatgpt": {"chat_base_url": "https://chatgpt.com", "auth_base_url": "https://auth.openai.com"}, "registration": {}}),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session", return_value=mock_browser) as create_session,
        patch("sms_tool.registration_drivers.playwright._wait_for_challenge_clear"),
        patch("sms_tool.registration_drivers.playwright._session_payload", return_value={"body": {}, "access_token": "new_at", "id_token": ""}),
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "status": "active", "status_code": 200}),
        patch("sms_tool.session_refresh._save_refreshed", return_value="session.json"),
    ):
        result = account_recovery.relogin_browser_session_account(account)

    assert result["ok"]
    # Must use the driver from browser_identity, not the default camoufox
    assert create_session.call_args.args[0] == "cloak"
    # Must pass browser_identity so the same profile is reopened
    assert create_session.call_args.kwargs["browser_identity"] == {"driver": "cloak", "profile_id": "browser@example.com"}


def test_refresh_local_quota_statuses_uses_browser_fetch_when_browser_identity_present():
    """Liveness probe routes through browser context when browser_identity is present."""
    from unittest.mock import MagicMock, patch

    account = {
        "email": "browser@example.com",
        "access_token": "at_123",
        "identity_context": create_registration_identity(
            "http://proxy.example:8080",
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
            account_key="browser@example.com",
            browser_identity={"driver": "camoufox", "profile_id": "browser@example.com"},
        ),
    }

    mock_browser = MagicMock()
    mock_browser.fetch_json = MagicMock(return_value={"status_code": 200, "body": {}})
    mock_browser.page = MagicMock()
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_browser)
    mock_session.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session", return_value=mock_session) as create_session,
        patch("sms_tool.registration_drivers.playwright._wait_for_challenge_clear"),
        patch.object(account_recovery, "probe_account_liveness", wraps=account_recovery.probe_account_liveness) as probe,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["browser@example.com"])

    assert result["ok"]
    # Must have opened a browser session with the saved driver
    assert create_session.call_args.args[0] == "camoufox"
    assert create_session.call_args.kwargs.get("browser_identity") == {
        "driver": "camoufox",
        "profile_id": "browser@example.com",
    }
    # Must have passed browser_fetch to probe_account_liveness
    assert probe.call_args.kwargs.get("browser_fetch") is not None


def test_refresh_local_quota_statuses_falls_back_to_curl_when_no_browser_identity():
    """Liveness probe uses curl_cffi when no browser_identity is present."""
    from unittest.mock import patch

    account = {
        "email": "plain@example.com",
        "access_token": "at_123",
        "identity_context": create_registration_identity(
            "http://proxy.example:8080",
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
        ),
    }

    with (
        patch.object(account_recovery, "get_account_record", return_value=account),
        patch("sms_tool.registration_drivers.external_sessions.create_browser_session") as create_session,
        patch.object(account_recovery, "probe_account_liveness", return_value={"ok": True, "quota_status": "active"}) as probe,
        patch.object(account_recovery, "mark_quota_status", return_value=True),
    ):
        result = account_recovery.refresh_local_quota_statuses(["plain@example.com"])

    assert result["ok"]
    # Must NOT have opened a browser session
    create_session.assert_not_called()
    # Must NOT have passed browser_fetch
    assert "browser_fetch" not in probe.call_args.kwargs or probe.call_args.kwargs.get("browser_fetch") is None
