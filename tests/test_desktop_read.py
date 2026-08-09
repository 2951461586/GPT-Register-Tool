import json
from pathlib import Path

from sms_tool.desktop_read import (
    create_account_file,
    create_mailbox_file,
    create_payment_url_file,
    read_account,
    read_accounts,
)
from sms_tool.storage import upsert_account


def _config(tmp_path: Path) -> dict:
    return {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }


def _seed(tmp_path: Path) -> tuple[dict, Path]:
    session = {
        "email": "reader@example.test",
        "password": "account-password",
        "success": True,
        "status": "registered",
        "access_token": "access-secret-value",
        "refresh_token": "refresh-secret-value",
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
        assert paths[1].read_text(encoding="utf-8").strip().startswith("remail://reader@example.test|")
        assert paths[2].read_text(encoding="utf-8").strip() == session["paypal"]["url"]
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
