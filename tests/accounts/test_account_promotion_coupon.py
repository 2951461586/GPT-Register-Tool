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


def test_not_eligible_coupon_overrides_the_campaign_hint(monkeypatch):
    """A hint-only "可试用" is not reproducible; ``not_eligible`` is the answer.

    Measured 2026-11-05 on the iCloud pool: 11 accounts reported
    ``可试用Plus -100% ×1month`` from ``eligible_promo_campaigns`` alone, and all
    11 answered ``not_eligible`` (``redeemed: False``) on re-probe through nine
    IN exits plus the identical batch path.  Before this, only a *redeemed*
    coupon demoted the hint, so those accounts kept advertising a trial.
    """

    def fake_get(url, **_kwargs):
        if "check_coupon" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"state": "not_eligible", "redemption": {"redeemed": False}},
            )
        return SimpleNamespace(
            status_code=200,
            json=lambda: _accounts_body(
                campaign={
                    "metadata": {"discount": {"percentage": 100}, "duration": {"num_periods": 1, "period": "month"}}
                }
            ),
        )

    account = {"access_token": "at", "chatgpt_account_id": "acc"}
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get):
        result = account_promotion.check_account_promotion(account, proxy="http://exit.example:8080", coupon_probe=True)

    assert result["ok"] is True
    assert result["coupon_state"] == "not_eligible"
    assert result["plus_trial_eligible"] is False
    assert "可试用" not in result["promotion_status"]


def test_unconfirmed_coupon_never_advertises_a_trial(monkeypatch):
    """A hint the coupon did not confirm must not reach the badge.

    Two measured false-positive classes are pinned here: an unrecognised/empty
    coupon state, and a coupon probe that never completes (``curl: (35)`` through
    residential exits is common).  Both leave ``plus_trial_hint`` set for
    diagnostics and ``plus_trial_eligible`` False.
    """

    def fake_get(url, **_kwargs):
        if "check_coupon" in url:
            return SimpleNamespace(status_code=200, json=lambda: {})
        return SimpleNamespace(
            status_code=200,
            json=lambda: _accounts_body(campaign={"metadata": {"discount": {"percentage": 100}}}),
        )

    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get):
        result = account_promotion.check_account_promotion(
            {"access_token": "at", "chatgpt_account_id": "acc"},
            proxy="http://exit.example:8080",
            coupon_probe=True,
        )

    assert result["coupon_state"] == ""
    assert result["plus_trial_eligible"] is False
    assert result["plus_trial_hint"] is True
    assert "可试用" not in result["promotion_status"]


def test_coupon_probe_uses_dynamic_campaign_id_from_accounts_check(monkeypatch):
    """P0-2: non-default campaigns (e.g. plus-2-months-50-pct-off) must probe their own id."""
    seen_urls: list[str] = []

    def fake_get(url, **_kwargs):
        seen_urls.append(url)
        if "check_coupon" in url:
            if "coupon=plus-2-months-50-pct-off" in url:
                return SimpleNamespace(status_code=200, json=lambda: {"state": "eligible"})
            return SimpleNamespace(status_code=200, json=lambda: {"state": "not_eligible"})
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "accounts": {
                    "default": {
                        "account": {"plan_type": "free", "account_id": "acc"},
                        "entitlement": {"has_active_subscription": False},
                        "eligible_promo_campaigns": {
                            "plus": {
                                "id": "plus-2-months-50-pct-off",
                                "metadata": {
                                    "title": "Plus 50% off",
                                    "summary": "2 months at 50% off",
                                    "promotion_type_label": "discount",
                                    "discount": {"percentage": 50},
                                    "duration": {"num_periods": 2, "period": "month"},
                                },
                            },
                            "go": {"id": "go-1-month-free", "metadata": {"title": "Go Trial"}},
                        },
                    }
                }
            },
        )

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get):
        result = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", coupon_probe=True
        )

    assert result["ok"] is True
    assert result["plus_trial_eligible"] is True
    assert result["coupon_campaign_id"] == "plus-2-months-50-pct-off"
    assert result["plus_trial_summary"] == "2 months at 50% off"
    assert result["plus_trial_promotion_type_label"] == "discount"
    assert set(result["eligible_promo_campaigns"]) == {"plus", "go"}
    assert any("coupon=plus-2-months-50-pct-off" in url for url in seen_urls)
    assert "可试用Plus·-50%·×2month" == result["promotion_status"]


def _accounts_body_with_personal_and_workspace(*, plan="free", active=False, plus_campaign=None):
    personal = {
        "account": {
            "account_id": "11111111-1111-1111-1111-111111111111",
            "structure": "personal",
            "plan_type": plan,
        },
        "entitlement": {"has_active_subscription": active},
    }
    if plus_campaign is not None:
        personal["eligible_promo_campaigns"] = {"plus": plus_campaign}
    workspace = {
        "account": {
            "account_id": "22222222-2222-2222-2222-222222222222",
            "structure": "workspace",
            "plan_type": "team",
        },
        "entitlement": {"has_active_subscription": True},
    }
    return {
        "accounts": {
            "22222222-2222-2222-2222-222222222222": workspace,
            "11111111-1111-1111-1111-111111111111": personal,
            "default": personal,
        }
    }


def test_team_coupon_probe_detects_5_seat_trial_with_personal_account_id(monkeypatch):
    """Probes team-1-month-free using the personal account's ChatGPT-Account-ID."""
    seen: list[tuple[str, dict]] = []

    def fake_get(url, **kwargs):
        seen.append((url, dict(kwargs.get("headers") or {})))
        if "coupon=team-1-month-free" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "coupon": "team-1-month-free",
                    "state": "eligible",
                    "redemption": {"redeemed": False},
                },
            )
        if "check_coupon" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"state": "not_eligible"})
        return SimpleNamespace(status_code=200, json=lambda: _accounts_body_with_personal_and_workspace())

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get):
        result = account_promotion.check_account_promotion(
            {"access_token": "at"},
            proxy="http://exit.example:8080",
            coupon_probe=True,
            team_coupon_probe=True,
        )

    assert result["ok"] is True
    assert result["personal_account_id"] == "11111111-1111-1111-1111-111111111111"
    assert result["plus_trial_eligible"] is False
    assert result["team_trial_eligible"] is True
    assert result["team_trial_state"] == "eligible"
    assert result["team_trial_campaign_id"] == "team-1-month-free"
    assert result["promotion_status"] == "可试用Team(5席)"
    assert result["promotion_state"] == "trial_eligible"
    team_calls = [(u, h) for u, h in seen if "coupon=team-1-month-free" in u]
    assert len(team_calls) == 1
    assert "is_coupon_from_query_param=true" in team_calls[0][0]
    assert team_calls[0][1]["Chatgpt-Account-Id"] == "11111111-1111-1111-1111-111111111111"


def test_team_coupon_probe_combines_with_plus_trial_and_subscribed_plan(monkeypatch):
    def fake_get(url, **_kwargs):
        if "coupon=team-1-month-free" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"coupon": "team-1-month-free", "state": "eligible", "redemption": {"redeemed": False}},
            )
        if "check_coupon" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"state": "eligible"})
        return SimpleNamespace(
            status_code=200,
            json=lambda: _accounts_body_with_personal_and_workspace(
                plus_campaign={
                    "id": "plus-1-month-free",
                    "metadata": {
                        "discount": {"percentage": 100},
                        "duration": {"num_periods": 1, "period": "month"},
                    },
                }
            ),
        )

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get):
        both = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", coupon_probe=True, team_coupon_probe=True
        )
    assert both["plus_trial_eligible"] is True
    assert both["team_trial_eligible"] is True
    assert both["promotion_status"] == "可试用Plus·-100%·×1month·可试用Team(5席)"
    assert both["promotion_state"] == "trial_eligible"

    def fake_get_subscribed(url, **_kwargs):
        if "coupon=team-1-month-free" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"coupon": "team-1-month-free", "state": "eligible", "redemption": {"redeemed": False}},
            )
        return SimpleNamespace(
            status_code=200,
            json=lambda: _accounts_body_with_personal_and_workspace(plan="plus", active=True),
        )

    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_get_subscribed):
        sub = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", coupon_probe=True, team_coupon_probe=True
        )
    assert sub["team_trial_eligible"] is True
    assert sub["promotion_status"] == "已订阅·Plus·可试用Team(5席)"
    assert sub["promotion_state"] == "trial_eligible"


def test_team_coupon_probe_handles_not_eligible_mismatch_and_missing_personal(monkeypatch):
    def fake_not_eligible(url, **_kwargs):
        if "coupon=team-1-month-free" in url:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "coupon": "team-1-month-free",
                    "state": "not_eligible",
                    "ineligibility_reason": "prior_introductory_offer_redeemed",
                    "redemption": {"redeemed": True},
                },
            )
        return SimpleNamespace(status_code=200, json=lambda: _accounts_body_with_personal_and_workspace())

    monkeypatch.setattr(account_promotion, "CFG", {})
    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_not_eligible):
        res = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", team_coupon_probe=True
        )
    assert res["team_trial_eligible"] is False
    assert res["team_trial_state"] == "not_eligible"
    assert res["team_trial_redeemed"] is True
    assert res["team_trial_ineligibility_reason"] == "prior_introductory_offer_redeemed"
    assert res["team_trial_ineligibility_reason_label"] == "此前已兑换过入门优惠"
    assert res["promotion_status"] == "Free·无优惠"

    def fake_mismatch(url, **_kwargs):
        if "coupon=team-1-month-free" in url:
            return SimpleNamespace(status_code=200, json=lambda: {"coupon": "other-campaign", "state": "eligible"})
        return SimpleNamespace(status_code=200, json=lambda: _accounts_body_with_personal_and_workspace())

    with patch.object(account_promotion.curl_requests, "get", side_effect=fake_mismatch):
        mismatch = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", team_coupon_probe=True
        )
    assert mismatch["team_trial_eligible"] is False
    assert mismatch["team_coupon"]["error"] == "team_coupon_campaign_mismatch"

    workspace_only = {
        "data": {
            "accounts": {
                "22222222-2222-2222-2222-222222222222": {
                    "account": {"structure": "workspace", "plan_type": "team"},
                    "entitlement": {"has_active_subscription": True},
                }
            }
        }
    }
    with patch.object(
        account_promotion.curl_requests,
        "get",
        return_value=SimpleNamespace(status_code=200, json=lambda: workspace_only),
    ) as get_mock:
        no_personal = account_promotion.check_account_promotion(
            _account(), proxy="http://exit.example:8080", team_coupon_probe=True
        )
    assert get_mock.call_count == 1
    assert no_personal["personal_account_id"] == ""
    assert no_personal["team_trial_eligible"] is False
    assert no_personal["team_coupon"]["error"] == "personal_account_not_found"


def test_promotion_batch_enables_team_coupon_probe_and_counts_team_trials(monkeypatch):
    from sms_tool.accounts import promotion_batch

    seen_kwargs: list[dict] = []

    def fake_check(account, **kwargs):
        seen_kwargs.append(dict(kwargs))
        return {
            "ok": True,
            "current_plan_type": "free",
            "plus_trial_eligible": False,
            "team_trial_eligible": True,
            "team_trial_state": "eligible",
            "promotion_status": "可试用Team(5席)",
            "promotion_state": "trial_eligible",
        }

    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    monkeypatch.setattr(promotion_batch, "check_account_promotion", fake_check)

    report = promotion_batch.refresh_promotion_statuses(["team@example.com"], workers=1)

    assert seen_kwargs and seen_kwargs[0].get("coupon_probe") is True
    assert seen_kwargs[0].get("team_coupon_probe") is True
    assert report["trial_eligible"] == 1
    assert report["team_trial_eligible"] == 1
    assert report["results"][0]["team_trial_eligible"] is True
    assert report["results"][0]["promotion_status"] == "可试用Team(5席)"
