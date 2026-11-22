import json
import time
from argparse import Namespace
from pathlib import Path

from sms_tool.accounts.account_email_change import (
    EmailChangeRequest,
    change_email_batch,
    change_email_begin,
    change_email_verify,
)
from sms_tool.mailbox.types import MailboxAccount
from sms_tool.commands.email_change import run_change_email
from sms_tool.storage import init_database, migrate_account_email, upsert_account, list_account_records


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_change_email_request_contract():
    calls = []

    def transport(path, payload, headers, timeout, proxy):
        calls.append((path, payload))
        return Response({"success": True, "eligible": True})

    account = {"email": "old@example.com", "cookie_header": "session=redacted"}
    assert change_email_begin(account, "new@example.com", transport=transport)["ok"]
    assert change_email_verify(account, "new@example.com", "123456", transport=transport)["ok"]
    assert calls[0][1] == {"email": "new@example.com"}
    assert calls[1][1] == {"email": "new@example.com", "code": "123456"}


def test_migrate_account_email_is_atomic_and_updates_session(tmp_path):
    db = tmp_path / "accounts.sqlite3"
    session = tmp_path / "session_old.json"
    session.write_text(json.dumps({"email": "old@example.com", "access_token": "old-at"}), encoding="utf-8")
    cfg = {"chatgpt": {}, "storage": {"sqlite_path": str(db)}}
    init_database(runtime_config=cfg)
    upsert_account(
        {
            "email": "old@example.com",
            "success": True,
            "status": "registered",
            "access_token": "old-at",
            "json_path": str(session),
            "mailbox": {"email": "old@example.com", "provider": "graph"},
        },
        runtime_config=cfg,
    )
    assert migrate_account_email(
        "old@example.com",
        "new@example.com",
        {
            "email": "new@example.com",
            "access_token": "new-at",
            "cookie_header": "session=new",
            "json_path": str(session),
            "mailbox": {"email": "new@example.com", "provider": "icloud"},
        },
        runtime_config=cfg,
    )
    rows = list_account_records(runtime_config=cfg)
    assert [row["email"] for row in rows] == ["new@example.com"]
    assert rows[0]["access_token"] == "new-at"
    assert json.loads(session.read_text(encoding="utf-8"))["email"] == "new@example.com"
    assert not migrate_account_email("new@example.com", "other@example.com", {}, runtime_config=cfg)


def test_batch_keeps_order_and_runs_concurrently(monkeypatch):
    accounts = [{"email": "a@example.com"}, {"email": "b@example.com"}]
    targets = [MailboxAccount("ta@example.com", provider="smailr"), MailboxAccount("tb@example.com", provider="smailr")]
    request = EmailChangeRequest(provider="smailr", workers=2)
    seen = []

    def one(account, target, request, **kwargs):
        seen.append((account["email"], target.email))
        return {"ok": True, "email": account["email"], "target_email": target.email}

    monkeypatch.setattr("sms_tool.accounts.account_email_change.change_one_account", one)
    result = change_email_batch(accounts, request, allocate=lambda req, count: targets)
    assert result["ok"] and result["success"] == 2
    assert [item["email"] for item in result["results"]] == ["a@example.com", "b@example.com"]
    assert set(seen) == {("a@example.com", "ta@example.com"), ("b@example.com", "tb@example.com")}


def test_cli_adapter_maps_arguments_to_request(tmp_path):
    email_file = tmp_path / "emails.txt"
    email_file.write_text("a@example.com\n", encoding="utf-8")
    emitted = []
    captured = []
    args = Namespace(
        change_email_provider="icloud",
        email_file=str(email_file),
        email="",
        change_email_mailbox_file="mailboxes.txt",
        mailbox_file="",
        change_email_workers=3,
        workers=1,
        change_email_timeout=120,
        change_email_otp_timeout=240,
        proxy="http://proxy:8080",
        change_email_service_mode="purchase",
        change_email_smailr_domain="",
        cfworker_domain="",
        desktop_ipc=True,
    )

    def batch(accounts, request):
        captured.append((accounts, request))
        return {"ok": True, "total": 1, "success": 1, "failed": 0, "results": []}

    run_change_email(
        args,
        load_accounts=lambda emails: [{"email": emails[0]}],
        change_email_batch=batch,
        request_type=EmailChangeRequest,
        emit_result=lambda payload, enabled: emitted.append((payload, enabled)),
    )

    assert captured[0][0] == [{"email": "a@example.com"}]
    assert captured[0][1].provider == "icloud"
    assert captured[0][1].workers == 3
    assert emitted[0][0]["ok"] is True
    assert emitted[0][1] is True


def test_email_change_headers_use_canonical_chatgpt_headers_not_hardcoded_chrome():
    from sms_tool.accounts.account_email_change import _chat_headers
    from sms_tool.transport.auth_headers import CHATGPT_CLIENT_VERSION, set_fingerprint_geo

    set_fingerprint_geo("JP")
    try:
        headers = _chat_headers(
            {
                "email": "old@example.com",
                "device_id": "did-change-1",
                "access_token": "tok-1",
                "cookie_header": "session=redacted",
                "registration_country": "JP",
            },
            "/backend-api/accounts/change_email/eligibility",
        )
    finally:
        set_fingerprint_geo("US")

    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["user-agent"] != "Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36"
    assert lowered["oai-client-version"] == CHATGPT_CLIENT_VERSION
    assert "oai-client-build-number" not in lowered
    assert lowered["oai-language"] == "ja-JP"
    assert lowered["oai-device-id"] == "did-change-1"
    assert lowered["authorization"] == "Bearer tok-1"
    assert lowered["cookie"] == "session=redacted"


def test_email_change_default_transport_uses_protocol_backend_and_tls(monkeypatch):
    from sms_tool.accounts import account_email_change

    calls = []

    class FakeSession:
        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return Response({"success": True, "eligible": True})

        def close(self):
            calls.append(("close", "", {}))

    tls_applied = []
    monkeypatch.setattr(account_email_change, "_new_protocol_session", lambda proxy="": FakeSession())
    monkeypatch.setattr(account_email_change, "_apply_tls_fingerprint", lambda session, cfg: tls_applied.append(True))
    monkeypatch.setattr(account_email_change, "auth_impersonate", lambda: "chrome146")

    result = account_email_change.check_change_email_eligibility(
        {"email": "old@example.com", "cookie_header": "session=ok"},
        proxy="http://127.0.0.1:8080",
    )
    assert result["ok"] is True
    assert tls_applied == [True]
    assert calls[0][2]["impersonate"] == "chrome146"
    assert calls[-1][0] == "close"
