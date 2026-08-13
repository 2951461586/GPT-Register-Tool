"""Tests for accounts/check plan + promotion (优惠) parsing and labels."""

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


def test_parse_missing_accounts():
    assert parse_accounts_check({})["ok"] is False
