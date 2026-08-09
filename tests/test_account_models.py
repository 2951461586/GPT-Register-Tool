import json

from sms_tool.account_models import AccountSessionModel
from sms_tool import storage


def test_account_session_model_hides_credentials_and_emits_safe_snapshot():
    model = AccountSessionModel.from_value({
        "email": "typed@example.com",
        "success": True,
        "access_token": "eyJsecret.payload.signature",
        "oauth_refresh_token": "rt_secret",
        "totp_secret": "TOTPSECRET",
        "mailbox": {"provider": "remail", "token": "mail-secret"},
        "paypal": {"ok": True, "url": "https://pay.example/?ba_token=BA-secret", "card_last4": "4242"},
    })

    assert "rt_secret" not in repr(model)
    assert not hasattr(model, "payload")
    assert not hasattr(model, "payload")
    snapshot = json.dumps(model.safe_snapshot())
    assert "rt_secret" not in snapshot
    assert "BA-secret" not in snapshot
    assert "4242" not in snapshot


def test_storage_accepts_typed_model_and_raw_json_is_token_free(tmp_path, monkeypatch):
    database = tmp_path / "accounts.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda cfg=None: database)
    model = AccountSessionModel.from_value({
        "email": "typed-storage@example.com",
        "success": True,
        "access_token": "at-secret-value",
        "oauth_refresh_token": "rt_secret_value",
    })

    assert storage.upsert_account(model)
    with storage._connect() as connection:
        row = connection.execute(
            "SELECT access_token, oauth_refresh_token, raw_json FROM accounts WHERE email=?",
            ("typed-storage@example.com",),
        ).fetchone()
    assert row["access_token"] == "at-secret-value"
    assert row["oauth_refresh_token"] == "rt_secret_value"
    assert "at-secret-value" not in row["raw_json"]
    assert "rt_secret_value" not in row["raw_json"]
