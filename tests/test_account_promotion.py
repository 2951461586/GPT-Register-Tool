"""Tests for accounts/check plan + promotion (优惠) parsing and labels."""

from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli
from sms_tool import account_promotion
from sms_tool.account_promotion import parse_accounts_check, promotion_status_label


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
