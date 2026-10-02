"""Read-only Plus-trial coupon probe merged into the promotion check (2026-10-01 scan P1-1).

``accounts/check`` only exposes an indirect ``eligible_promo_campaigns`` hint.
The coupon endpoint (``/backend-api/promo_campaign/check_coupon``) answers the
trial question directly; the probe is read-only and never redeems anything.  The
batch path opts in via ``coupon_probe=True``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sms_tool.accounts import account_promotion
from sms_tool.accounts.account_promotion import parse_coupon_check


def _accounts_body(*, plan="free", active=False, campaign=None):
    item = {
        "account": {"plan_type": plan, "account_id": "acc"},
        "entitlement": {"has_active_subscription": active},
    }
    if campaign is not None:
        item["eligible_promo_campaigns"] = {"plus": campaign}
    return {"accounts": {"default": item}}


def _account():
    return {"access_token": "at", "chatgpt_account_id": "acc"}


def test_parse_coupon_check_eligible():
    parsed = parse_coupon_check({"state": "eligible"})
    assert parsed["coupon_trial_eligible"] is True
    assert parsed["coupon_redeemed"] is False
    assert parsed["coupon_state"] == "eligible"


def test_parse_coupon_check_redeemed():
    parsed = parse_coupon_check(
        {"status": "redeemed", "redemption": {"redeemed": True, "user_redeemed_at": "2026-01-02T03:04:05Z"}}
    )
    assert parsed["coupon_redeemed"] is True
    assert parsed["coupon_trial_eligible"] is False
    assert parsed["coupon_redeemed_at"] == "2026-01-02T03:04:05Z"


def test_parse_coupon_check_tolerates_garbage():
    assert parse_coupon_check(None)["coupon_state"] == ""
    assert parse_coupon_check("not-json")["coupon_trial_eligible"] is False
    assert parse_coupon_check({"redemption": "nope"})["coupon_redeemed"] is False


def test_coupon_probe_promotes_trial_eligibility(monkeypatch):
    def fake_get(url, **_kwargs):
        if "check_coupon" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"state": "eligible"})
        return SimpleNamespace(status_code=200, json=lambda: _accounts_body())

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get) as get:
        result = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", coupon_probe=True
        )

    assert result["ok"] is True
    assert result["plus_trial_eligible"] is True
    assert result["coupon_state"] == "eligible"
    assert "可试用Plus" in result["promotion_status"]
    assert get.call_count == 2


def test_redeemed_coupon_overrides_the_campaign_hint(monkeypatch):
    """A redeemed coupon is the negative answer even when accounts/check still lists the campaign."""

    def fake_get(url, **_kwargs):
        if "check_coupon" in url:
            return SimpleNamespace(
                status_code=200, json=lambda: {"state": "redeemed", "redemption": {"redeemed": True}}
            )
        return SimpleNamespace(
            status_code=200,
            json=lambda: _accounts_body(campaign={"id": "plus-1-month-free"}),
        )

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get):
        result = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", coupon_probe=True
        )

    assert result["coupon_redeemed"] is True
    assert result["plus_trial_eligible"] is False


def test_coupon_failure_never_overrides_the_plan_answer(monkeypatch):
    def fake_get(url, **_kwargs):
        if "check_coupon" in url:
            raise RuntimeError("coupon endpoint down")
        return SimpleNamespace(status_code=200, json=lambda: _accounts_body())

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get):
        result = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", coupon_probe=True
        )

    assert result["ok"] is True
    assert result["coupon"]["ok"] is False
    assert result["promotion_status"] == "Free·无优惠"


def test_coupon_probe_routes_through_browser_fetch_when_present(monkeypatch):
    seen: list[str] = []

    def fake_browser_fetch(url, *, headers=None, timeout_ms=None):
        seen.append(url)
        if "check_coupon" in url:
            return {"status": 200, "body": {"state": "eligible"}}
        return {"status": 200, "body": _accounts_body()}

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get") as curl_get:
        result = account_promotion.check_account_promotion(
            _account(), proxy=None, browser_fetch=fake_browser_fetch, coupon_probe=True
        )

    assert result["plus_trial_eligible"] is True
    assert len(seen) == 2
    curl_get.assert_not_called()


def test_coupon_probe_is_off_by_default(monkeypatch):
    """Single-account callers keep the historical one-request behaviour."""

    def fake_get(url, **_kwargs):
        return SimpleNamespace(status_code=200, json=lambda: _accounts_body())

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get) as get:
        result = account_promotion.check_account_promotion(_account(), proxy="http://exit.example:8080")

    assert result["ok"] is True
    assert "coupon_state" not in result
    assert get.call_count == 1
