"""Tests for offline-JWT lightweight account inspection (--inspect-accounts)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from sms_tool import cli
from sms_tool.accounts.account_inspection import (
    access_token_needs_refresh,
    decode_jwt_exp,
    inspect_accounts_with_runner,
    partition_accounts_for_inspection,
)
from sms_tool.commands import accounts as account_commands


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{header}.{body}.sig"


def test_access_token_needs_refresh_classifies_jwt_expiry():
    now = 1_700_000_000.0
    valid_jwt = _make_jwt({"exp": int(now + 3600)})
    expiring_jwt = _make_jwt({"exp": int(now + 120)})
    expired_jwt = _make_jwt({"exp": int(now - 60)})
    no_exp_jwt = _make_jwt({"sub": "user-1"})
    bool_exp_jwt = _make_jwt({"exp": True})

    assert decode_jwt_exp(valid_jwt) == float(int(now + 3600))
    assert decode_jwt_exp(f"Bearer {valid_jwt}") == float(int(now + 3600))
    assert decode_jwt_exp(bool_exp_jwt) is None

    assert access_token_needs_refresh("", now=now) == (True, "missing_token")
    assert access_token_needs_refresh("not-a-jwt", now=now) == (True, "malformed_token")
    assert access_token_needs_refresh(no_exp_jwt, now=now) == (True, "malformed_token")
    assert access_token_needs_refresh(expired_jwt, now=now) == (True, "expired")
    assert access_token_needs_refresh(expiring_jwt, now=now, skew_seconds=300) == (True, "expiring_soon")
    assert access_token_needs_refresh(expiring_jwt, now=now, skew_seconds=60) == (False, "valid")
    assert access_token_needs_refresh(valid_jwt, now=now, skew_seconds=300) == (False, "valid")


def test_partition_accounts_skips_valid_and_terminal_but_keeps_marked_invalid():
    now = 1_700_000_000.0
    valid_jwt = _make_jwt({"exp": int(now + 7200)})
    expired_jwt = _make_jwt({"exp": int(now - 10)})

    accounts = [
        {"email": "valid@example.test", "access_token": valid_jwt, "status": "ok"},
        {"email": "deactivated@example.test", "access_token": expired_jwt, "status": "account_deactivated"},
        {
            "email": "revoked@example.test",
            "access_token": expired_jwt,
            "terminal_failure": {"code": "token_revoked"},
        },
        {"email": "expired@example.test", "access_token": expired_jwt},
        {"email": "missing@example.test", "access_token": ""},
        {"email": "revoked_early@example.test", "access_token": valid_jwt, "status": "at_invalid"},
    ]

    candidates, skipped_valid, skipped_deactivated = partition_accounts_for_inspection(accounts, now=now)

    assert [item["email"] for item in skipped_valid] == ["valid@example.test"]
    assert [item["email"] for item in skipped_deactivated] == [
        "deactivated@example.test",
        "revoked@example.test",
    ]
    assert [(item["email"], item["reason"]) for item in candidates] == [
        ("expired@example.test", "expired"),
        ("missing@example.test", "missing_token"),
        ("revoked_early@example.test", "marked_invalid"),
    ]


def test_inspect_accounts_makes_zero_network_calls_when_pool_is_all_valid():
    now = 1_700_000_000.0
    valid_jwt = _make_jwt({"exp": int(now + 7200)})
    quota_calls: list[dict] = []
    promo_calls: list[dict] = []

    report = inspect_accounts_with_runner(
        ["a@example.test", "b@example.test"],
        load_accounts_fn=lambda _emails: [
            {"email": "a@example.test", "access_token": valid_jwt},
            {"email": "b@example.test", "access_token": valid_jwt},
        ],
        refresh_quota_fn=lambda **kw: quota_calls.append(kw) or {"ok": True, "results": []},
        refresh_promotion_fn=lambda **kw: promo_calls.append(kw) or {"ok": True},
        now=now,
    )

    assert quota_calls == []
    assert promo_calls == []
    assert report["ok"] is True
    assert report["total"] == 2
    assert report["skipped_valid"] == 2
    assert report["inspected"] == 0
    assert report["recovered"] == 0
    assert report["failed"] == 0


def test_inspect_accounts_recovers_only_candidates_and_refreshes_promotions_for_recovered():
    now = 1_700_000_000.0
    valid_jwt = _make_jwt({"exp": int(now + 7200)})
    expired_jwt = _make_jwt({"exp": int(now - 5)})
    quota_calls: list[dict] = []
    promo_calls: list[dict] = []

    def fake_quota(**kwargs):
        quota_calls.append(kwargs)
        return {
            "ok": False,
            "results": [
                {"email": "expired@example.test", "ok": True, "relogin": {"ok": True}},
                {"email": "broken@example.test", "ok": False, "error": "otp_timeout"},
            ],
        }

    def fake_promo(**kwargs):
        promo_calls.append(kwargs)
        return {"ok": True, "total": len(kwargs["emails"]), "success": len(kwargs["emails"])}

    report = inspect_accounts_with_runner(
        load_accounts_fn=lambda _emails: [
            {"email": "healthy@example.test", "access_token": valid_jwt},
            {"email": "dead@example.test", "access_token": expired_jwt, "status": "account_deactivated"},
            {"email": "expired@example.test", "access_token": expired_jwt},
            {"email": "broken@example.test", "access_token": ""},
        ],
        refresh_quota_fn=fake_quota,
        refresh_promotion_fn=fake_promo,
        now=now,
    )

    assert len(quota_calls) == 1
    assert quota_calls[0]["emails"] == ["expired@example.test", "broken@example.test"]
    assert quota_calls[0]["relogin_on_401"] is True
    assert len(promo_calls) == 1
    assert promo_calls[0]["emails"] == ["expired@example.test"]
    assert report["ok"] is False
    assert report["total"] == 4
    assert report["skipped_valid"] == 1
    assert report["skipped_deactivated"] == 1
    assert report["inspected"] == 2
    assert report["recovered"] == 1
    assert report["failed"] == 1
    assert report["success"] == 2


def test_inspect_accounts_cli_flags_and_early_dispatch(monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--inspect-accounts", "--inspect-skew-seconds", "600", "--inspect-no-promotion", "--email", "u@example.test"]
    )
    assert args.inspect_accounts is True
    assert args.inspect_skew_seconds == 600
    assert args.inspect_no_promotion is True

    called: list[object] = []
    monkeypatch.setattr(cli, "_inspect_accounts", lambda parsed: called.append(parsed))
    assert cli._main_early_commands(args) is True
    assert called == [args]

    captured: list[dict] = []

    def fake_run(**kwargs):
        captured.append(kwargs)
        return {
            "ok": False,
            "total": 1,
            "skipped_valid": 0,
            "skipped_deactivated": 0,
            "inspected": 1,
            "recovered": 0,
            "failed": 1,
        }

    monkeypatch.setattr(account_commands, "_load_recovery_batch", lambda: (None, fake_run))
    ctx = account_commands.AccountCommandContext(list_paypal_accounts=lambda **_: [], get_paypal_url=lambda _e: "")
    with pytest.raises(SystemExit) as exc:
        account_commands.inspect_accounts(
            SimpleNamespace(
                email_file=None,
                email="u@example.test",
                quota_workers=2,
                workers=4,
                proxy=None,
                proxy_pool="",
                refresh_timeout=30,
                inspect_skew_seconds=600,
                quota_relogin_timeout=None,
                scan_relogin_mode="auto",
                quota_batch_timeout=None,
                quota_account_timeout=None,
                inspect_no_promotion=True,
                desktop_ipc=False,
            ),
            ctx,
        )
    assert exc.value.code == 3
    assert captured[0]["emails"] == ["u@example.test"]
    assert captured[0]["skew_seconds"] == 600
    assert captured[0]["refresh_promotion"] is False
