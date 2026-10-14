"""One-click SMS failure persistence is owned by the OAuth workflow."""

import json
from pathlib import Path
from unittest.mock import patch

from sms_tool import codex_oauth
from sms_tool.commands import one_click


def test_failed_oauth_preserves_session_and_db_with_atomic_file_write(tmp_path):
    session = tmp_path / "session.json"
    session.write_text('{"email": "e@example.test", "access_token": "at"}', encoding="utf-8")
    writes = []

    def upsert(data, *, json_path):
        writes.append((data, json_path))
        return True

    result = codex_oauth.persist_one_click_sms_failure(
        {"access_token": "at", "response": {}}, str(session), "e@example.test",
        {"error": "phone_sms_timeout", "tokens": {"refresh_token": "secret"}},
        upsert=upsert,
    )

    stored = json.loads(session.read_text(encoding="utf-8"))
    assert stored["email"] == "e@example.test"
    assert "secret" not in json.dumps(stored)
    assert stored["response"]["codex_oauth"]["has_refresh_token"] is True
    assert writes[0][1] == str(session)
    assert result == {"session_saved": True, "account_saved": True, "persisted": True}


def test_failed_oauth_does_not_persist_raw_response_bodies(tmp_path):
    session = tmp_path / "session.json"
    codex_oauth.persist_one_click_sms_failure(
        {}, str(session), "e@example.test",
        {
            "error": "phone_sms_timeout",
            "body": "private-auth-code",
            "phone_attempt": {"ok": False, "error": "sms_timeout", "phone": "1234",
                              "body": "private-supplier-reply", "next_url": "private-redirect"},
        },
        upsert=lambda data, *, json_path: True,
    )
    stored = json.loads(session.read_text(encoding="utf-8"))
    assert "private-auth-code" not in json.dumps(stored)
    assert "private-supplier-reply" not in json.dumps(stored)
    assert "private-redirect" not in json.dumps(stored)
    assert stored["response"]["phone_verification"]["error"] == "sms_timeout"


def test_failed_session_write_reports_partial_success_and_preserves_existing_file(tmp_path):
    session = tmp_path / "session.json"
    original = '{"email":"e@example.test","access_token":"old"}'
    session.write_text(original, encoding="utf-8")
    writes = []

    with patch("sms_tool.codex_oauth.atomic_write_text", side_effect=OSError("permission denied")):
        result = codex_oauth.persist_one_click_sms_failure(
            {"access_token": "at"}, str(session), "e@example.test",
            {"error": "phone_sms_timeout"},
            upsert=lambda data, *, json_path: writes.append(data) or True,
        )

    assert session.read_text(encoding="utf-8") == original
    assert len(writes) == 1
    assert result == {
        "session_saved": False,
        "account_saved": True,
        "persisted": False,
        "error_code": "session_write_failed",
    }


def test_unserializable_session_is_reported_as_partial_save(tmp_path):
    result = codex_oauth.persist_one_click_sms_failure(
        {"non_json_value": object()}, str(tmp_path / "session.json"), "e@example.test",
        {"error": "phone_sms_timeout"},
        upsert=lambda data, *, json_path: True,
    )
    assert result["session_saved"] is False
    assert result["account_saved"] is True
    assert result["error_code"] == "session_write_failed"


def test_failed_db_write_reports_partial_success(tmp_path):
    session = tmp_path / "session.json"
    result = codex_oauth.persist_one_click_sms_failure(
        {}, str(session), "e@example.test", {"error": "phone_sms_timeout"},
        upsert=lambda data, *, json_path: False,
    )
    assert session.is_file()
    assert result == {
        "session_saved": True,
        "account_saved": False,
        "persisted": False,
        "error_code": "account_write_failed",
    }


def test_successful_oauth_reports_failed_db_save_without_losing_remote_success(tmp_path):
    session = tmp_path / "session.json"
    with patch.object(codex_oauth, "upsert_account", return_value=False):
        result = codex_oauth._save_oauth_tokens(
            {"email": "e@example.test"}, str(session),
            {"access_token": "at", "refresh_token": "rt"},
            "e@example.test", "codex_oauth_pkce",
        )

    assert result["ok"] is True
    assert result["persistence"] == {
        "session_saved": True, "account_saved": False,
        "persisted": False, "error_code": "account_write_failed",
    }
    assert result["persisted"] is False
    assert session.is_file()


def test_successful_oauth_attempts_db_save_after_session_write_failure(tmp_path):
    with (
        patch.object(codex_oauth, "atomic_write_text", side_effect=OSError("permission denied")),
        patch.object(codex_oauth, "upsert_account", return_value=True) as upsert,
    ):
        result = codex_oauth._save_oauth_tokens(
            {"email": "e@example.test"}, str(tmp_path / "session.json"),
            {"access_token": "at", "refresh_token": "rt"},
            "e@example.test", "codex_oauth_pkce",
        )

    assert result["ok"] is True
    assert result["persistence"] == {
        "session_saved": False, "account_saved": True,
        "persisted": False, "error_code": "session_write_failed",
    }
    upsert.assert_called_once()


def test_command_adapter_does_not_write_session_files():
    source = Path(one_click.__file__).read_text(encoding="utf-8")
    assert "write_text(" not in source
    assert "atomic_write_text(" not in source
