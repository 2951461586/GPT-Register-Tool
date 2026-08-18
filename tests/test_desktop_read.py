import base64
import json
from pathlib import Path

from sms_tool.desktop_read import (
    create_account_file,
    create_mailbox_file,
    create_payment_url_file,
    read_account,
    read_accounts,
    read_mailbox_pool,
)
from sms_tool.storage import upsert_account


def _config(tmp_path: Path) -> dict:
    return {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }


def _access_token(plan_type: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({
        "https://api.openai.com/auth": {"chatgpt_plan_type": plan_type},
    }).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


def _seed(tmp_path: Path) -> tuple[dict, Path]:
    session = {
        "email": "reader@example.test",
        "password": "account-password",
        "success": True,
        "status": "registered",
        "access_token": "access-secret-value",
        "refresh_token": "rt_refresh-secret-value",
        "totp_secret": "totp-secret-value",
        "mailbox": {
            "email": "reader@example.test",
            "provider": "remail",
            "source": "purchase",
            "token": "mailbox-secret-value",
            "purchase_id": "purchase-fixture",
        },
        "paypal": {
            "ok": True,
            "status": "link_ready",
            "url": "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE-SECRET",
        },
    }
    session_path = tmp_path / "session_reader.json"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    assert upsert_account(session, json_path=str(session_path), runtime_config=_config(tmp_path))
    return session, session_path


def test_public_desktop_reads_expose_presence_not_credentials(tmp_path):
    session, _ = _seed(tmp_path)

    rows = read_accounts(_config(tmp_path))
    detail = read_account(email=session["email"], runtime_config=_config(tmp_path))

    assert len(rows) == 1
    assert detail["has_access_token"] is True
    assert detail["has_refresh_token"] is True
    assert detail["has_payment_url"] is True
    assert detail["access_token_present"] is True
    assert detail["refresh_token_present"] is True
    assert detail["payment_url_present"] is True
    rendered = json.dumps({"rows": rows, "detail": detail})
    for secret in (
        "access-secret-value",
        "refresh-secret-value",
        "totp-secret-value",
        "mailbox-secret-value",
        "BA-FIXTURE-SECRET",
    ):
        assert secret not in rendered
    for forbidden_key in ("access_token", "refresh_token", "totp_secret", "paypal_url"):
        assert forbidden_key not in detail


def test_sensitive_exports_use_temporary_files(tmp_path):
    session, _ = _seed(tmp_path)

    account_result = create_account_file(email=session["email"], runtime_config=_config(tmp_path))
    mailbox_result = create_mailbox_file(email=session["email"], runtime_config=_config(tmp_path))
    payment_result = create_payment_url_file(email=session["email"], runtime_config=_config(tmp_path))
    paths = [Path(item["path"]) for item in (account_result, mailbox_result, payment_result)]
    try:
        exported = json.loads(paths[0].read_text(encoding="utf-8"))
        assert exported["access_token"] == session["access_token"]
        assert paths[1].read_text(encoding="utf-8").strip().startswith("remail://reader@example.test---")
        assert paths[2].read_text(encoding="utf-8").strip() == session["paypal"]["url"]
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_public_read_prefers_current_token_plan_over_stale_database(tmp_path):
    session = {
        "email": "upgraded@example.test",
        "success": True,
        "access_token": "old-token",
        "auth_session": {"account": {"planType": "free"}},
    }
    session_path = tmp_path / "session_upgraded.json"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    assert upsert_account(session, json_path=str(session_path), runtime_config=_config(tmp_path))

    session["access_token"] = _access_token("plus")
    session_path.write_text(json.dumps(session), encoding="utf-8")

    row = read_account(email=session["email"], runtime_config=_config(tmp_path))
    assert row["account_type"] == "plus"


def test_public_read_exposes_latest_access_token_probe_status_code(tmp_path):
    session = {
        "email": "invalid-at@example.test",
        "success": False,
        "status": "at_invalid",
        "access_token": "expired-access-token",
        "account_scan": {
            "token_probe": {"status": "token_invalid", "status_code": 401},
        },
    }
    session_path = tmp_path / "session_invalid_at.json"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    assert upsert_account(session, json_path=str(session_path), runtime_config=_config(tmp_path))

    row = read_account(email=session["email"], runtime_config=_config(tmp_path))

    assert row["access_token_present"] is True
    assert row["at_probe_status_code"] == "401"


def test_smailr_mailbox_line_falls_back_to_source_id(tmp_path):
    source = json.dumps({
        "address": "reuse@smailr.com",
        "id": "b2432eb0-2bd2-43a3-93ac-20c57ff4f76e",
        "user_id": "ffcaec5b-5e12-4bbc-9ec4-000000000000",
        "mail_count": 4,
    })
    session = {
        "email": "reuse@smailr.com",
        "password": "account-password",
        "success": True,
        "status": "registered",
        "mailbox": {
            "email": "reuse@smailr.com",
            "provider": "smailr",
            "source": source,
        },
    }
    session_path = tmp_path / "session_reuse.json"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    assert upsert_account(session, json_path=str(session_path), runtime_config=_config(tmp_path))

    result = create_mailbox_file(email="reuse@smailr.com", runtime_config=_config(tmp_path))
    assert result["ok"] is True
    line = Path(result["path"]).read_text(encoding="utf-8").strip()
    assert line == "smailr://reuse@smailr.com---b2432eb0-2bd2-43a3-93ac-20c57ff4f76e"


def test_public_read_prefers_newer_relogin_probe_over_stale_401(tmp_path):
    session = {
        "email": "relogin@example.test",
        "success": False,
        "status": "at_invalid",
        "access_token": "old-access-token",
        "account_scan_updated_at": 100,
        "account_scan": {
            "token_probe": {"status": "token_invalid", "status_code": 401},
        },
    }
    session_path = tmp_path / "session_relogin.json"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    assert upsert_account(session, json_path=str(session_path), runtime_config=_config(tmp_path))

    # Relogin has updated the session file, while SQLite still contains the
    # previous 401 snapshot.
    session.update({
        "success": True,
        "status": "registered",
        "access_token": "new-access-token",
        "quota_updated_at": 200,
        "quota": {
            "updated_at": 200,
            "last_result": {"status": "active", "status_code": 200},
        },
    })
    session_path.write_text(json.dumps(session), encoding="utf-8")

    row = read_account(email=session["email"], runtime_config=_config(tmp_path))

    assert row["at_probe_status_code"] == "200"


def _pool_config(tmp_path: Path, token_name: str = "mailbox_tokens.txt") -> dict:
    config = _config(tmp_path)
    config["email_registration"] = {"token_file": token_name}
    return config


def test_mailbox_pool_parses_every_supported_format(tmp_path):
    (tmp_path / "mailbox_tokens.txt").write_text("\n".join([
        "# comment",
        "cfworker://worker@example.test",
        "bare@edu.liziai.cloud",
        "remail://remail@example.test---service-token---order-1---purchase-9",
        "smailr://smailr@example.test---mailbox-id",
        "user@icloud.com----https://mail.example/inbox/private-token",
        "gmail://app@gmail.com---app-password",
        "gmail://oauth@gmail.com----client-id----client-secret----refresh-token",
        "chatai@example.test----password----9f8b4c2a-1234-4bcd-8efa-0123456789ab----chatai-refresh",
        "graph@example.test---password---graph-refresh---graph-access---0",
        "",
    ]), encoding="utf-8")

    result = read_mailbox_pool(_pool_config(tmp_path), root_dir=tmp_path)

    assert len(result["files"]) == 1
    pool_file = result["files"][0]
    assert pool_file["name"] == "mailbox_tokens.txt"
    lines = pool_file["lines"]
    assert [line["provider"] for line in lines] == [
        "cfworker", "cfworker", "remail", "smailr", "icloud_url",
        "gmail", "gmail", "chatai", "graph",
    ]
    by_email = {line["email"]: line for line in lines}
    assert by_email["worker@example.test"]["raw_line"] == "cfworker://worker@example.test"
    assert by_email["bare@edu.liziai.cloud"]["raw_line"] == "bare@edu.liziai.cloud"
    remail = by_email["remail@example.test"]
    assert remail["token"] == "service-token"
    assert remail["order_no"] == "order-1"
    assert remail["purchase_id"] == "purchase-9"
    assert by_email["smailr@example.test"]["token"] == "mailbox-id"
    assert by_email["user@icloud.com"]["token"] == "https://mail.example/inbox/private-token"
    assert by_email["user@icloud.com"]["auth_mode"] == "otp_url"
    gmail_app = by_email["app@gmail.com"]
    assert gmail_app["password"] == "app-password"
    assert gmail_app["auth_mode"] == "app_password"
    assert gmail_app["client_id"] == ""
    gmail_oauth = by_email["oauth@gmail.com"]
    assert gmail_oauth["client_id"] == "client-id"
    assert gmail_oauth["client_secret"] == "client-secret"
    assert gmail_oauth["refresh_token"] == "refresh-token"
    assert gmail_oauth["auth_mode"] == "oauth_refresh"
    chatai = by_email["chatai@example.test"]
    assert chatai["client_id"] == "9f8b4c2a-1234-4bcd-8efa-0123456789ab"
    assert chatai["refresh_token"] == "chatai-refresh"
    graph = by_email["graph@example.test"]
    assert graph["refresh_token"] == "graph-refresh"
    assert graph["access_token"] == "graph-access"


def test_mailbox_pool_skips_malformed_lines(tmp_path):
    (tmp_path / "mailbox_tokens.txt").write_text("\n".join([
        "remail://missing-order---service-token",
        "smailr://missing-id",
        "gmail://",
        "not-an-email----password----client----refresh",
        "two---parts",
        "graph@example.test---password---",
        "ok@example.test---password---refresh",
    ]), encoding="utf-8")

    result = read_mailbox_pool(_pool_config(tmp_path), root_dir=tmp_path)

    lines = result["files"][0]["lines"]
    assert [line["email"] for line in lines] == ["ok@example.test"]
    assert lines[0]["line_no"] == 7


def test_mailbox_pool_tolerates_missing_files(tmp_path):
    result = read_mailbox_pool(
        _pool_config(tmp_path),
        extra_files=(str(tmp_path / "absent_selected.txt"),),
        root_dir=tmp_path,
    )

    assert result == {"files": []}


def test_mailbox_pool_enumerates_known_files_once(tmp_path):
    selected = tmp_path / "selected.txt"
    selected.write_text("sel@example.test---password---refresh", encoding="utf-8")
    (tmp_path / "mailbox_tokens.txt").write_text(
        "tok@example.test---password---refresh", encoding="utf-8")
    (tmp_path / "hotmail.txt").write_text(
        "hot@example.test----password----client----refresh", encoding="utf-8")
    (tmp_path / "my_chatai_export.txt").write_text(
        "cfworker://glob@example.test", encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignored@example.test---p---r", encoding="utf-8")

    result = read_mailbox_pool(
        _pool_config(tmp_path),
        extra_files=(str(selected), str(tmp_path / "mailbox_tokens.txt")),
        root_dir=tmp_path,
    )

    names = [entry["name"] for entry in result["files"]]
    assert names == ["selected.txt", "mailbox_tokens.txt", "hotmail.txt", "my_chatai_export.txt"]
    emails = {
        entry["name"]: [line["email"] for line in entry["lines"]]
        for entry in result["files"]
    }
    assert emails["selected.txt"] == ["sel@example.test"]
    assert emails["mailbox_tokens.txt"] == ["tok@example.test"]
    assert emails["hotmail.txt"] == ["hot@example.test"]
    assert emails["my_chatai_export.txt"] == ["glob@example.test"]
