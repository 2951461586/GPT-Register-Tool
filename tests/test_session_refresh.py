import json
from unittest.mock import patch

from sms_tool import session_refresh


def test_protocol_candidate_can_be_returned_without_persistence():
    data = {"email": "ok@example.com", "cookie_header": "__Secure-next-auth.session-token=cookie"}
    auth_session = {
        "accessToken": "new_at",
        "refreshToken": "rt_new",
        "user": {"email": "ok@example.com"},
    }
    with (
        patch.object(session_refresh, "_fetch_protocol_auth_session", return_value=auth_session),
        patch.object(session_refresh, "_save_refreshed") as save,
    ):
        result = session_refresh._refresh_session_protocol(
            data,
            "session.json",
            "ok@example.com",
            30,
            persist=False,
        )

    assert result["ok"]
    assert not result["persisted"]
    assert result["data"]["access_token"] == "new_at"
    assert result["data"]["oauth_refresh_token"] == "rt_new"
    save.assert_not_called()


def test_auth_session_email_reads_nested_session_user():
    assert session_refresh._auth_session_email({"session": {"user": {"email": "User@Example.com"}}}) == "user@example.com"


def test_browser_refresh_paths_are_removed():
    # Browser-based re-login was deleted; only the protocol path remains.
    assert not hasattr(session_refresh, "_refresh_session_browser")
    assert not hasattr(session_refresh, "_complete_browser_email_login")
    assert "browser" not in session_refresh.refresh_session.__doc__.lower() or "removed" in session_refresh.refresh_session.__doc__.lower()


def test_explicit_session_file_rehydrates_flattened_mailbox_credentials(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({
        "email": "liziai@smailr.com",
        "mailbox": {"email": "liziai@smailr.com", "provider": "smailr"},
    }), encoding="utf-8")
    record = {
        "email": "liziai@smailr.com",
        "mailbox_provider": "smailr",
        "mailbox_source": "purchase",
        "mailbox_token": "mailbox-token",
        "mailbox_refresh_token": "",
    }

    with patch.object(session_refresh, "get_account_record", return_value=record):
        data, json_path = session_refresh._load_seed_session(session_file=str(path))

    assert json_path == str(path)
    assert data["mailbox_provider"] == "smailr"
    assert data["mailbox_source"] == "purchase"
    assert data["mailbox_token"] == "mailbox-token"
