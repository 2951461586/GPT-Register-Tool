"""Tests for accounts/check plan + promotion (优惠) parsing and labels."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool import cli
from sms_tool.accounts import account_promotion
from sms_tool.accounts.account_promotion import parse_accounts_check, promotion_status_label
from sms_tool.accounts.account_identity import create_registration_identity
from sms_tool.desktop_read import read_account
from sms_tool.storage import get_account_record, mark_promotion_status, mark_quota_status, upsert_account


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


def test_refresh_promotion_statuses_rotates_stateless_proxy_after_timeout(monkeypatch):
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {
        "email": email,
        "access_token": "at",
        "raw_json": json.dumps({"access_token": "at"}),
    })
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    calls = []

    def probe(account, **kwargs):
        calls.append(kwargs["proxy"])
        if len(calls) == 1:
            return {
                "ok": False,
                "promotion_status": "检测失败",
                "error": "Failed to perform, curl: (28) Connection timed out",
            }
        return {"ok": True, "promotion_status": "Free·无优惠"}

    monkeypatch.setattr(account_promotion, "check_account_promotion", probe)
    result = account_promotion.refresh_promotion_statuses(
        ["rotate@example.com"],
        workers=1,
        proxy="http://dead.example:8080",
        proxy_pool="http://dead.example:8080\nhttp://healthy.example:8080",
        timeout=5,
    )

    assert result["success"] == 1
    assert len(calls) == 2
    assert set(calls) == {"http://dead.example:8080", "http://healthy.example:8080"}


def test_parse_missing_accounts():
    assert parse_accounts_check({})["ok"] is False


def _trial_probe(**overrides):
    probe = {
        "ok": True,
        "current_plan_type": "free",
        "has_active_subscription": False,
        "plus_trial_eligible": True,
        "plus_trial_discount_percentage": 100,
        "plus_trial_duration_num_periods": 1,
        "plus_trial_duration_period": "month",
        "promotion_status": "可试用Plus·-100%·×1month",
    }
    probe.update(overrides)
    return probe


def test_trial_label_appends_payment_methods_when_present():
    label = promotion_status_label(_trial_probe(payment_methods_label="银行卡/MoMo/UPI"))
    assert label == "可试用Plus·-100%·×1month｜可支付:银行卡/MoMo/UPI"


def test_trial_label_unchanged_without_payment_methods():
    assert promotion_status_label(_trial_probe()) == "可试用Plus·-100%·×1month"
    # Non-trial results never carry the suffix even if a stale label lingers.
    paid = {"ok": True, "current_plan_type": "plus", "has_active_subscription": True,
            "payment_methods_label": "银行卡"}
    assert "可支付" not in promotion_status_label(paid)


def test_payment_method_display_names():
    assert account_promotion.payment_method_display("card") == "银行卡"
    assert account_promotion.payment_method_display("momo") == "MoMo"
    assert account_promotion.payment_method_display("upi") == "UPI"
    assert account_promotion.payment_method_display("gcash") == "GCash"
    assert account_promotion.payment_method_display("kakao") == "Kakao Pay"
    assert account_promotion.payment_method_display("some_future_wallet") == "some_future_wallet"
    assert account_promotion.payment_method_display("") == ""


def test_refresh_probes_payment_methods_for_trial_accounts(monkeypatch):
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {
        "email": email,
        "access_token": "at",
        "registration_country": "VN",
        "raw_json": json.dumps({"access_token": "at", "registration_country": "VN"}),
    })
    persisted = {}
    monkeypatch.setattr(
        "sms_tool.storage.mark_promotion_status",
        lambda email, label, **kwargs: persisted.update(email=email, label=label, probe=kwargs.get("promotion_result")) or True,
    )
    monkeypatch.setattr(
        account_promotion, "check_account_promotion",
        lambda account, **kwargs: _trial_probe(),
    )
    capability_calls = []

    def fake_capability_probe(token, method, **kwargs):
        capability_calls.append({"method": method, **kwargs})
        return {
            "ok": True,
            "ordered_payment_method_types": ["card", "momo"],
            "payment_method_types": ["card", "momo", "upi"],
            "custom_payment_methods": [],
            "checkout_country": "VN",
            "currency": "VND",
        }

    monkeypatch.setattr(
        "sms_tool.payment_capability.payment_method_capability_probe", fake_capability_probe
    )

    result = account_promotion.refresh_promotion_statuses(["trial@example.com"], workers=1)

    assert result["success"] == 1
    assert len(capability_calls) == 1
    assert capability_calls[0]["method"] == "direct_card"
    assert capability_calls[0]["billing_country"] == "VN"
    assert capability_calls[0]["currency"] == "VND"
    assert persisted["label"] == "可试用Plus·-100%·×1month｜可支付:银行卡/MoMo/UPI"
    assert persisted["probe"]["payment_methods"] == ["card", "momo", "upi"]


def test_refresh_skips_payment_probe_for_non_trial_accounts(monkeypatch):
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        account_promotion, "check_account_promotion",
        lambda account, **kwargs: {"ok": True, "promotion_status": "Free·无优惠", "plus_trial_eligible": False},
    )
    called = []
    monkeypatch.setattr(
        "sms_tool.payment_capability.payment_method_capability_probe",
        lambda *args, **kwargs: called.append(1) or {},
    )

    result = account_promotion.refresh_promotion_statuses(["free@example.com"], workers=1)

    assert result["success"] == 1
    assert result["trial_eligible"] == 0
    assert called == []


def test_payment_probe_failure_keeps_the_trial_label(monkeypatch):
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    persisted = {}
    monkeypatch.setattr(
        "sms_tool.storage.mark_promotion_status",
        lambda email, label, **kwargs: persisted.update(label=label) or True,
    )
    monkeypatch.setattr(
        account_promotion, "check_account_promotion",
        lambda account, **kwargs: _trial_probe(),
    )

    def boom(*args, **kwargs):
        raise RuntimeError("checkout_transport_failed")

    monkeypatch.setattr("sms_tool.payment_capability.payment_method_capability_probe", boom)

    result = account_promotion.refresh_promotion_statuses(["trial@example.com"], workers=1)

    assert result["success"] == 1
    assert persisted["label"] == "可试用Plus·-100%·×1month"


def test_post_registration_promotion_stage_deduplicates_and_counts_trials():
    result = {
        "ok": True,
        "total": 2,
        "success": 2,
        "failed": 0,
        "trial_eligible": 1,
        "results": [
            {"email": "one@example.com", "promotion_status": "可试用Plus", "probe": {"plus_trial_eligible": True}},
            {"email": "two@example.com", "promotion_status": "Free·无优惠", "probe": {"plus_trial_eligible": False}},
        ],
    }
    with patch("sms_tool.accounts.account_promotion.refresh_promotion_statuses", return_value=result) as refresh:
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


def test_post_registration_promotion_stage_forwards_proxy_pool():
    result = {"ok": True, "total": 0, "success": 0, "failed": 0, "trial_eligible": 0, "results": []}
    with patch("sms_tool.accounts.account_promotion.refresh_promotion_statuses", return_value=result) as refresh:
        cli._check_registered_promotions(
            ["one@example.com"],
            proxy=None,
            proxy_pool=["http://pool-a:8080", "http://pool-b:8080"],
        )

    assert refresh.call_args.kwargs["proxy_pool"] == ["http://pool-a:8080", "http://pool-b:8080"]


def test_refresh_reports_trial_eligible_for_trial_accounts(monkeypatch):
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        account_promotion, "check_account_promotion",
        lambda account, **kwargs: _trial_probe(),
    )
    monkeypatch.setattr(
        "sms_tool.payment_capability.payment_method_capability_probe",
        lambda *args, **kwargs: {},
    )

    result = account_promotion.refresh_promotion_statuses(["trial@example.com"], workers=1)

    assert result["trial_eligible"] == 1


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


def _browser_identity_account(driver="cloak"):
    registration_proxy = "http://proxy.example:8080"
    return {
        "access_token": "at",
        "chatgpt_account_id": "acc",
        "identity_context": create_registration_identity(
            registration_proxy,
            pool_index=0,
            fingerprint_key="chrome146",
            device_id="device-123",
            account_key="browser@example.com",
            browser_identity={"driver": driver, "profile_id": "browser@example.com"},
        ),
    }, {"proxy": {"registration": registration_proxy, "pool": [registration_proxy]}}


def test_promotion_normalizes_browser_fetch_status_key():
    """Regression: ``fetch_json`` returns ``{"status", "body"}``, not ``{"status_code"}``.

    ``PlaywrightBrowserSession.fetch_json`` — inherited by the anti-detect
    drivers (cloak/roxy/camoufox) — returns the HTTP code under ``status``.
    Reading only ``status_code`` silently degraded every browser-routed probe to
    "HTTP 0", so promotion checks on browser-registered accounts always
    reported HTTP 0 regardless of the real response.  ``account_liveness``
    already normalized this; promotion did not.
    """
    account, config = _browser_identity_account()

    def fake_browser_fetch(url, *, headers=None, timeout_ms=None):
        # The REAL PlaywrightBrowserSession.fetch_json contract.
        return {
            "status": 200,
            "body": {
                "accounts": {
                    "default": {
                        "account": {"plan_type": "free", "account_id": "acc"},
                        "entitlement": {"has_active_subscription": False},
                    },
                },
            },
        }

    with patch.object(account_promotion, "CFG", config):
        result = account_promotion.check_account_promotion(
            account, proxy=None, browser_fetch=fake_browser_fetch
        )

    assert result["ok"], result
    assert result["status_code"] == 200
    assert result["promotion_status"] == "Free·无优惠"


def test_promotion_surfaces_real_browser_http_errors_not_zero():
    """A genuine 401 from the browser must surface as AT失效, not HTTP 0."""
    account, config = _browser_identity_account()

    def fake_browser_fetch(url, *, headers=None, timeout_ms=None):
        return {"status": 401, "body": {"error": {"message": "Could not parse your authentication token."}}}

    with patch.object(account_promotion, "CFG", config):
        result = account_promotion.check_account_promotion(
            account, proxy=None, browser_fetch=fake_browser_fetch
        )

    assert result["status_code"] == 401
    assert result["promotion_status"] == "AT失效"


def test_promotion_401_stays_in_promotion_namespace(tmp_path):
    config = {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }
    session_path = tmp_path / "session.json"
    session = {
        "email": "promotion-invalid@example.test",
        "access_token": "expired-at",
        "status": "registered",
        "success": True,
    }
    session_path.write_text(json.dumps(session), encoding="utf-8")
    assert upsert_account(session, json_path=str(session_path), runtime_config=config)

    assert mark_promotion_status(
        session["email"],
        "AT失效",
        {"ok": False, "status_code": 401, "error": "token_invalid"},
        runtime_config=config,
    )

    record = get_account_record(session["email"], runtime_config=config)
    assert record["status"] == "registered"
    public = read_account(email=session["email"], runtime_config=config)
    assert public["promotion_status"] == "AT失效"
    assert public.get("at_probe_status_code", "") != "401"


def test_liveness_200_restores_shared_at_status_after_promotion_401(tmp_path):
    config = {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }
    session = {
        "email": "promotion-recovered@example.test",
        "access_token": "at",
        "status": "at_invalid",
        "success": True,
    }
    assert upsert_account(session, runtime_config=config)
    assert mark_quota_status(
        session["email"],
        "可用",
        {"ok": True, "status_code": 200, "status": "active"},
        runtime_config=config,
    )
    record = get_account_record(session["email"], runtime_config=config)
    assert record["status"] == "registered"
    assert record["quota_status"] == "可用"
def test_promotion_explicit_proxy_wins_over_pool():
    with patch.object(account_promotion, "parse_proxy_pool", return_value=["http://pool:1"]), \
         patch.object(account_promotion, "proxy_pool_for", return_value=["http://cfg:2"]):
        assert account_promotion._promotion_proxy_candidates(
            {"email": "a@example.com"}, "http://explicit:3", None
        ) == ["http://explicit:3", "http://pool:1"]


# ---------------------------------------------------------------------------
# 跨语言机器契约：与 tests/SmsWorkbench.Tests/PromotionStatusContractTests.cs
# 共用 tests/fixtures/promotion_status_cases.json，两侧任一漂移即失败。
# ---------------------------------------------------------------------------

from pathlib import Path as _Path

from sms_tool.accounts.account_promotion import promotion_status_code
from sms_tool.promotion_states import promotion_marker_is_stale

_PROMOTION_FIXTURE = _Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "promotion_status_cases.json"


def test_promotion_label_and_state_match_shared_contract():
    cases = json.loads(_PROMOTION_FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert cases, "fixture must not be empty"
    for case in cases:
        assert promotion_status_label(case["result"]) == case["label"], case["name"]
        assert promotion_status_code(case["result"]) == case["state"], case["name"]


def test_promotion_state_is_persisted_next_to_the_label(tmp_path):
    import sms_tool.store.markers as markers

    config = {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }
    assert upsert_account({"email": "state@example.test", "success": True}, runtime_config=config)
    assert markers.mark_promotion_status(
        "state@example.test",
        "可试用Plus·-100%·×1month",
        {"ok": True, "promotion_status": "可试用Plus·-100%·×1month", "promotion_state": "trial_eligible"},
        runtime_config=config,
    )
    record = get_account_record("state@example.test", runtime_config=config)
    data = json.loads(record["raw_json"])
    assert data["promotion_state"] == "trial_eligible"
    assert data["promotion"]["state"] == "trial_eligible"


def test_promotion_marker_is_stale_rule_is_single_owned():
    # 401 标记 + 后来 AT 200 ⇒ 标记过期；机器状态优先，旧记录回落到文案。
    assert promotion_marker_is_stale("AT失效", "", "200") is True
    assert promotion_marker_is_stale("", "auth_invalid", 200) is True
    assert promotion_marker_is_stale("AT失效", "", "") is False
    assert promotion_marker_is_stale("AT失效", "", "401") is False
    assert promotion_marker_is_stale("", "probe_failed", "200") is False
    assert promotion_marker_is_stale("Free·无优惠", "free", "200") is False
