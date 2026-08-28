"""Tests for accounts/check plan + promotion (优惠) parsing and labels."""

from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli
from sms_tool import account_promotion
from sms_tool.account_promotion import parse_accounts_check, promotion_status_label
from sms_tool.account_identity import create_registration_identity


def test_parse_plus_trial_eligible():
    body = {
        "accounts": {
            "default": {
                "account": {"plan_type": "free", "account_id": "acc"},
                "entitlement": {"subscription_plan": "chatgptfreeplan", "has_active_subscription": False},
                "eligible_promo_campaigns": {
                    "plus": {
                        "id": "camp",
                        "metadata": {
                            "discount": {"percentage": 70},
                            "duration": {"num_periods": 1, "period": "month"},
                            "title": "Plus trial",
                        },
                    }
                },
            }
        }
    }
    result = parse_accounts_check(body)
    assert result["ok"] and result["plus_trial_eligible"]
    assert result["current_plan_type"] == "free"
    label = promotion_status_label(result)
    assert "可试用Plus" in label and "70%" in label


def test_parse_paid_subscription():
    body = {
        "accounts": {
            "default": {
                "account": {"plan_type": "plus"},
                "entitlement": {"has_active_subscription": True, "subscription_plan": "chatgptplusplan"},
            }
        }
    }
    result = parse_accounts_check(body)
    assert result["ok"] and result["has_active_subscription"]
    assert "订阅" in promotion_status_label(result) or "Plus" in promotion_status_label(result)


def test_parse_free_without_promo():
    body = {"accounts": {"default": {"account": {"plan_type": "free"}, "entitlement": {"has_active_subscription": False}}}}
    result = parse_accounts_check(body)
    assert promotion_status_label(result) == "Free·无优惠"


def test_labels_for_failures():
    assert promotion_status_label({"ok": False, "error": "token_invalid"}) == "AT失效"
    assert promotion_status_label({"ok": False, "error": "boom"}) == "检测失败"


def test_promotion_uses_dedicated_health_proxy_with_account_fingerprint_and_device():
    base_proxy = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"
    registration_proxy = "http://user-region-US-sid-NEW5678-t-5:secret@proxy.example:443"
    health_proxy = "http://promotion.example:8000"
    config = {
        "proxy": {"registration": base_proxy, "pool": [base_proxy]},
        "account_health": {"proxies": {"promotion": [health_proxy]}},
    }
    account = {
        "access_token": "at",
        "chatgpt_account_id": "acc",
        "identity_context": create_registration_identity(
            registration_proxy,
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
        ),
    }
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free", "account_id": "acc"},
                    "entitlement": {"has_active_subscription": False},
                },
            },
        },
    )

    with patch.object(account_promotion, "CFG", config), patch.object(
        account_promotion.curl_requests,
        "get",
        return_value=response,
    ) as get:
        result = account_promotion.check_account_promotion(
            account,
            proxy="http://127.0.0.1:7897",
        )

    assert result["ok"]
    assert get.call_args.kwargs["proxies"]["https"] == health_proxy
    assert get.call_args.kwargs["impersonate"] == "chrome146"
    assert get.call_args.kwargs["headers"]["oai-device-id"] == "device-123"


def test_refresh_promotion_statuses_emits_terminal_event_per_account(monkeypatch):
    events = []
    monkeypatch.setenv("SMSWORKBENCH_EVENTS", "1")
    monkeypatch.setattr("sms_tool.desktop_ipc.emit_event", lambda payload, enabled=None: events.append(payload) or True)
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    monkeypatch.setattr(account_promotion, "check_account_promotion", lambda account, **kwargs: {"ok": True, "promotion_status": "Free·无优惠"})

    result = account_promotion.refresh_promotion_statuses(["a@example.com", "b@example.com"], workers=2)

    terminal = [event for event in events if event.get("stage") == "account_completed"]
    assert result["total"] == 2
    assert len(terminal) == 2
    assert {event["account_ref"] for event in terminal} == {"a@example.com", "b@example.com"}
    assert all(event["total"] == 2 for event in terminal)


def test_parse_missing_accounts():
    assert parse_accounts_check({})["ok"] is False


def test_post_registration_promotion_stage_deduplicates_and_counts_trials():
    result = {
        "ok": True,
        "total": 2,
        "success": 2,
        "failed": 0,
        "results": [
            {"email": "one@example.com", "promotion_status": "可试用Plus", "probe": {"plus_trial_eligible": True}},
            {"email": "two@example.com", "promotion_status": "Free·无优惠", "probe": {"plus_trial_eligible": False}},
        ],
    }
    with patch("sms_tool.account_promotion.refresh_promotion_statuses", return_value=result) as refresh:
        report = cli._check_registered_promotions(
            ["ONE@example.com", "one@example.com", "two@example.com"],
            workers=3,
            proxy="http://proxy.example:8080",
            timeout=17,
        )

    assert report["trial_eligible"] == 1
    assert refresh.call_args.kwargs["emails"] == ["one@example.com", "two@example.com"]
    assert refresh.call_args.kwargs["workers"] == 3
    assert refresh.call_args.kwargs["timeout"] == 17


def test_registration_save_invokes_optional_promotion_stage(tmp_path):
    args = SimpleNamespace(
        registration_batch_id="batch-test",
        buy_remail_mailbox=False,
        remail_service_mode=None,
        check_promotion_after_registration=True,
        import_cpa=False,
        workers=4,
        proxy=None,
        refresh_timeout=20,
    )
    registration = {
        "success": True,
        "email": "new@example.com",
        "access_token": "test-access-token",
    }
    promotion = {"ok": True, "total": 1, "success": 1, "failed": 0, "trial_eligible": 1, "results": []}

    with patch.object(cli, "CFG", {"output": {"filename_pattern": "session_{email}_{timestamp}.json"}}), \
         patch.object(cli, "upsert_account", return_value=True), \
         patch.object(cli, "database_path", return_value=tmp_path / "accounts.sqlite3"), \
         patch("sms_tool.storage.record_registration_audit"), \
         patch.object(cli, "_check_registered_promotions", return_value=promotion) as check:
        report = cli._save_registration_results(
            args,
            [registration],
            effective_count=1,
            base_dir=tmp_path,
            pipeline_started=0,
            mailbox_seconds=0,
            register_seconds=1,
        )

    check.assert_called_once()
    assert check.call_args.args[0] == ["new@example.com"]
    assert report["promotion"] == promotion


def test_promotion_uses_browser_fetch_when_browser_identity_present():
    """Browser-registered accounts route promotion through the browser context."""
    registration_proxy = "http://proxy.example:8080"
    config = {"proxy": {"registration": registration_proxy, "pool": [registration_proxy]}}
    account = {
        "access_token": "at",
        "chatgpt_account_id": "acc",
        "identity_context": create_registration_identity(
            registration_proxy,
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
            account_key="browser@example.com",
            browser_identity={"driver": "playwright", "profile_id": "browser@example.com"},
        ),
    }
    browser_response = {
        "status_code": 200,
        "body": {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free", "account_id": "acc"},
                    "entitlement": {"has_active_subscription": False},
                },
            },
        },
    }

    def fake_browser_fetch(url, *, headers=None, timeout_ms=None):
        return browser_response

    with patch.object(account_promotion, "CFG", config), patch.object(
        account_promotion.curl_requests,
        "get",
    ) as curl_get:
        result = account_promotion.check_account_promotion(
            account,
            proxy="http://127.0.0.1:7897",
            browser_fetch=fake_browser_fetch,
        )

    assert result["ok"]
    assert result["promotion_status"] == "Free·无优惠"
    # curl_cffi must NOT be called when browser_fetch is provided
    curl_get.assert_not_called()
