"""Offline unit tests for native GPT Team 5-seat trial checkout link extraction."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool.pay_link.team_trial import (
    TEAM_TRIAL_PLAN_NAME,
    build_team_trial_checkout_payload,
    currency_for_country,
    extract_team_trial_link,
    normalize_team_seat_quantity,
    normalize_team_workspace_name,
    probe_team_trial_eligibility,
)
from sms_tool.payment import link_manager as manager
from sms_tool.payment.promotion_states import TEAM_TRIAL_CAMPAIGN_ID


def _fake_jwt(account_id: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode("ascii").rstrip("=")
    body = (
        base64.urlsafe_b64encode(
            json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"{header}.{body}.sig"


def _resp(status_code: int, payload: dict | str):
    if isinstance(payload, dict):
        return SimpleNamespace(status_code=status_code, json=lambda: payload, text=json.dumps(payload))
    return SimpleNamespace(
        status_code=status_code, json=lambda: (_ for _ in ()).throw(ValueError("not json")), text=payload
    )


def test_payload_and_parameter_normalization():
    assert normalize_team_seat_quantity(5) == 5
    assert normalize_team_seat_quantity(1) == 2
    assert normalize_team_seat_quantity(99) == 5
    assert normalize_team_seat_quantity("bad") == 5
    assert normalize_team_workspace_name("  AlphaTeam  ") == "AlphaTeam"
    assert normalize_team_workspace_name("") == "MyTeam"
    assert currency_for_country("jp") == "JPY"
    assert currency_for_country("zz") == "USD"

    payload = build_team_trial_checkout_payload(country="us", workspace_name="Demo", seat_quantity=5)
    assert payload["plan_name"] == TEAM_TRIAL_PLAN_NAME
    assert payload["team_plan_data"] == {"workspace_name": "Demo", "price_interval": "month", "seat_quantity": 5}
    assert payload["billing_details"] == {"country": "US", "currency": "USD"}
    assert payload["checkout_ui_mode"] == "hosted"
    assert payload["promo_campaign"] == {
        "promo_campaign_id": TEAM_TRIAL_CAMPAIGN_ID,
        "is_coupon_from_query_param": True,
    }


def test_probe_team_trial_eligibility_eligible_and_ineligible():
    token = _fake_jwt("11111111-2222-3333-4444-555555555555")
    seen_headers: list[dict] = []

    def get_eligible(url, access_token, cookie, proxy, timeout, headers):
        seen_headers.append(dict(headers or {}))
        return _resp(200, {"coupon": "team-1-month-free", "state": "eligible", "redemption": {"redeemed": False}})

    res = probe_team_trial_eligibility(token, get_fn=get_eligible)
    assert res["ok"] is True
    assert res["eligible"] is True
    assert res["decision"] == "eligible"
    assert seen_headers[0]["Chatgpt-Account-Id"] == "11111111-2222-3333-4444-555555555555"

    def get_ineligible(url, access_token, cookie, proxy, timeout, headers):
        return _resp(
            200,
            {
                "coupon": "team-1-month-free",
                "state": "ineligible",
                "ineligibility_reason": "prior_introductory_offer_redeemed",
                "redemption": {"redeemed": True},
            },
        )

    res2 = probe_team_trial_eligibility(token, get_fn=get_ineligible)
    assert res2["ok"] is True
    assert res2["eligible"] is False
    assert res2["decision"] == "prior_introductory_offer_redeemed"


def test_extract_team_trial_link_happy_path_auto_detects_country():
    token = _fake_jwt("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    posted: list[dict] = []

    def fake_get(url, access_token, cookie, proxy, timeout, headers):
        if "check_coupon" in url:
            return _resp(200, {"coupon": "team-1-month-free", "state": "eligible"})
        if url.endswith("/backend-api/me"):
            return _resp(200, {"country": "JP"})
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, body, access_token, cookie, proxy, timeout, headers):
        posted.append({"url": url, "body": body, "headers": headers})
        return _resp(
            200,
            {
                "url": "https://checkout.stripe.com/c/pay/cs_live_team_demo_12345",
                "processor_entity": "openai_ie",
            },
        )

    result = extract_team_trial_link(
        token,
        workspace_name="LabTeam",
        seat_quantity=5,
        get_fn=fake_get,
        post_fn=fake_post,
    )
    assert result["ok"] is True
    assert result["payment_method"] == "team_trial"
    assert result["source"] == "team_trial_direct"
    assert result["url"] == "https://checkout.stripe.com/c/pay/cs_live_team_demo_12345"
    assert result["cs_id"] == "cs_live_team_demo_12345"
    assert result["target_country"] == "JP"
    assert result["currency"] == "JPY"
    assert result["seat_quantity"] == 5
    assert result["workspace_name"] == "LabTeam"
    assert posted[0]["body"]["billing_details"] == {"country": "JP", "currency": "JPY"}
    assert posted[0]["headers"]["Chatgpt-Account-Id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_extract_team_trial_link_fails_fast_when_ineligible():
    token = _fake_jwt("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    post_called = False

    def fake_get(url, access_token, cookie, proxy, timeout, headers):
        return _resp(
            200,
            {"coupon": "team-1-month-free", "state": "ineligible", "ineligibility_reason": "promo_ineligible"},
        )

    def fake_post(*args, **kwargs):
        nonlocal post_called
        post_called = True
        return _resp(200, {})

    result = extract_team_trial_link(token, get_fn=fake_get, post_fn=fake_post)
    assert result["ok"] is False
    assert result["error_code"] == "team_trial_ineligible"
    assert result["retryable"] is False
    assert post_called is False


def test_extract_team_trial_link_redacts_token_on_error():
    secret_token = "eyJSECRET.PAYLOAD.SIGN"

    def fake_get(url, access_token, cookie, proxy, timeout, headers):
        if "check_coupon" in url:
            return _resp(401, {"detail": f"invalid bearer {secret_token}"})
        return _resp(200, {})

    result = extract_team_trial_link(secret_token, get_fn=fake_get)
    assert result["ok"] is False
    assert result["error_code"] == "token_invalid"
    assert secret_token not in result["error"]


def test_manager_routes_team_trial_directly_without_extract_server():
    cfg = {
        "chatgpt": {},
        "protocol_payments": {
            "extract_link_server": {
                "enabled": False,
                "mode": "off",
                "api_base": "",
                "cdk": "",
            }
        },
    }
    expected = {
        "ok": True,
        "status": "completed",
        "operation": "extract_link",
        "payment_method": "team_trial",
        "source": "team_trial_direct",
        "link_type": "team_trial_hosted",
        "url": "https://checkout.stripe.com/c/pay/cs_live_direct_999",
        "target_country": "US",
        "currency": "USD",
    }
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
        patch("sms_tool.pay_link.team_trial.extract_team_trial_link", return_value=expected) as direct_exec,
    ):
        result = manager.generate_payment_link(
            "at-token",
            payment_method="team_trial",
            runtime_config=cfg,
            workspace_name="DirectTeam",
            seat_quantity=5,
        )

    assert result["ok"] is True
    assert result["source"] == "team_trial_direct"
    assert result["url"] == "https://checkout.stripe.com/c/pay/cs_live_direct_999"
    assert result["manager_state"] == "completed"
    direct_exec.assert_called_once()
    assert direct_exec.call_args.kwargs["workspace_name"] == "DirectTeam"
    assert direct_exec.call_args.kwargs["seat_quantity"] == 5


def test_manager_probe_only_routes_team_trial_to_coupon_probe():
    probe_result = {
        "ok": True,
        "status": "completed",
        "operation": "payment_method_capability_probe",
        "payment_method": "team_trial",
        "source": "team_trial_direct",
        "eligible": True,
        "decision": "eligible",
    }
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch("sms_tool.pay_link.persistence._state_path", return_value=Path(tmp) / "runs.jsonl"),
        patch("sms_tool.pay_link.team_trial.probe_team_trial_eligibility", return_value=probe_result) as probe,
    ):
        result = manager.generate_payment_link(
            "at-token",
            payment_method="team",
            probe_only=True,
            runtime_config={"chatgpt": {}, "protocol_payments": {}},
        )

    assert result["ok"] is True
    assert result["eligible"] is True
    probe.assert_called_once()
