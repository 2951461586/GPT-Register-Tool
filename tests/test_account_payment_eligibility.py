"""Tests for the post-registration payment-eligibility probe.

Two things are being protected here:

1. **The enumeration contract** -- one Checkout + Stripe init per account has to
   yield the *whole* payment-method list, resolved against the account's own
   billing country rather than whatever exit happened to be free.
2. **The persistence whitelist** -- ``AccountSessionModel.safe_snapshot()`` is a
   closed whitelist and ``upsert_account`` re-serializes raw_json from it, so a
   field that is not listed there is silently dropped by the next relogin or
   account-health pass.  That is exactly how the ``promotion*`` keys were lost
   on 2026-09-21 (three accounts went from 65 keys to 16), and the last test in
   this file is the guard against repeating it for ``payment_capability``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sms_tool.accounts.account_models import AccountSessionModel


@pytest.mark.parametrize("command,payment_flag", [
    ("check_payment_eligibility", False), ("check_promotion", True),
])
def test_explicit_checkout_commands_reject_missing_account_selection(
    monkeypatch, command, payment_flag,
):
    from sms_tool.commands import accounts

    monkeypatch.setattr(accounts, "read_email_file", lambda _path: [])
    args = SimpleNamespace(email_file="", email="", payment_eligibility=payment_flag)

    class NoAccountSweep:
        def list_paypal_accounts(self):
            pytest.fail("an explicit Checkout command must not sweep all accounts")

    with pytest.raises(SystemExit, match="--email"):
        getattr(accounts, command)(args, NoAccountSweep())
from sms_tool.accounts.account_payment_eligibility import (
    billing_country_for,
    billing_currency_for,
    payment_locale_for,
    probe_account_payment_eligibility,
)
from sms_tool import gen_pp_link
from sms_tool.promotion_states import (
    PAYMENT_ELIGIBILITY_UNKNOWN_LABEL,
    payment_eligibility_is_unknown,
    payment_eligibility_label,
    payment_method_tokens,
    promotion_status_with_eligibility,
)
from sms_tool.storage import get_account_record, mark_promotion_status, upsert_account


def _config(tmp_path: Path) -> dict:
    return {
        "chatgpt": {},
        "storage": {"sqlite_path": str(tmp_path / "accounts.sqlite3")},
        "runtime": {"directory": str(tmp_path)},
    }


@pytest.fixture(autouse=True)
def _offline_egress_gate():
    """These tests exercise the probe contract without contacting geo endpoints."""
    with patch("sms_tool.payment_egress.assert_egress_countries"):
        yield


# --------------------------------------------------------------------------
# Billing country / currency resolution
# --------------------------------------------------------------------------

def test_billing_country_prefers_the_registration_country():
    assert billing_country_for({"registration_country": "in"}) == "IN"
    assert billing_country_for({"registration_country": "PH"}) == "PH"


def test_billing_country_falls_back_to_us():
    assert billing_country_for({}) == "US"
    assert billing_country_for({"registration_country": ""}) == "US"
    assert billing_country_for({"registration_country": "INVALID"}) == "US"
    assert billing_country_for("not-a-mapping") == "US"


def test_billing_currency_covers_the_catalog_countries():
    assert billing_currency_for("US") == "USD"
    assert billing_currency_for("IN") == "INR"
    assert billing_currency_for("KR") == "KRW"
    # These live in the local extras because payment_wire.CURRENCY_MAP
    # predates the PH/VN/PL/CH/ES/NL catalog entries; adding them there would
    # have silently changed that lane's EUR fallback.
    assert billing_currency_for("VN") == "VND"
    assert billing_currency_for("PH") == "PHP"
    assert billing_currency_for("PL") == "PLN"
    assert billing_currency_for("CH") == "CHF"


def test_billing_currency_defaults_to_usd():
    assert billing_currency_for("ZZ") == "USD"
    assert billing_currency_for("") == "USD"


def test_payment_locale_follows_the_country():
    assert payment_locale_for("IN") == "en"
    assert payment_locale_for("ID") == "id"
    assert payment_locale_for("VN") == "vi"
    assert payment_locale_for("KR") == "ko"


# --------------------------------------------------------------------------
# Probe wiring
# --------------------------------------------------------------------------

def test_probe_without_access_token_never_calls_the_transport():
    with patch("sms_tool.payment_capability.payment_method_capability_probe") as probe:
        result = probe_account_payment_eligibility({"email": "e@example.test"})

    assert result["ok"] is False
    assert result["error_code"] == "missing_access_token"
    assert result["retryable"] is False
    probe.assert_not_called()


def test_probe_requests_the_account_billing_country_and_enumerates_every_method():
    captured: dict = {}

    def fake_probe(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "classification": "eligible",
            "eligible": True,
            "currency": "INR",
            "amount": 0,
            "offer_state": "zero_due",
            "payment_method_types": ["card", "link"],
            "ordered_payment_method_types": ["upi", "card", "link"],
            "custom_payment_methods": ["momo"],
        }

    account = {
        "email": "e@example.test",
        "access_token": "access-token",
        "registration_country": "IN",
        "device_id": "device-1",
        "cookie_header": "oai-did=device-1",
    }
    with patch("sms_tool.payment_capability.payment_method_capability_probe", fake_probe):
        result = probe_account_payment_eligibility(account, proxy="http://exit.test:80", timeout=30)

    assert captured["payment_method"] == "direct_card"
    assert captured["access_token"] == "access-token"
    assert captured["billing_country"] == "IN"
    assert captured["checkout_country"] == "IN"
    assert captured["currency"] == "INR"
    assert captured["payment_locale"] == "en"
    assert captured["browser_locale"] == "en-IN"
    assert captured["browser_timezone"] == "Asia/Kolkata"
    assert captured["proxy"] == "http://exit.test:80"
    assert captured["auth_context"]["oai_did"] == "device-1"

    # One probe, whole list: the carrier method is not what we are asking about,
    # so the answer must carry every group Stripe returned, ordered first.
    assert result["ok"] is True
    assert result["billing_country"] == "IN"
    assert result["methods"] == ["upi", "card", "link", "momo"]
    assert result["currency"] == "INR"


def test_account_probe_reuses_saved_identity_through_custom_checkout():
    account = {
        "email": "e@example.test",
        "access_token": "access-token",
        "registration_country": "IN",
        "device_id": "legacy-device",
        "identity_context": {"device_id": "saved-device"},
        "cookie_header": "oai-did=saved-device; session=fixture",
    }
    checkout = SimpleNamespace(
        status_code=200, json=lambda: {"checkout_session_id": "oaics_fixture"},
    )
    custom = SimpleNamespace(
        status_code=200, json=lambda: {"currency": "inr", "payment_method_types": ["upi"]},
    )
    with patch("sms_tool.accounts.account_payment_eligibility.payment_egress.assert_egress_countries"), \
         patch.object(gen_pp_link, "_checkout_post", return_value=checkout) as post, \
         patch.object(gen_pp_link, "_checkout_get", return_value=custom) as get:
        result = probe_account_payment_eligibility(account, proxy="http://exit.test:80")

    assert result["ok"] is True
    assert post.call_args.args[3:5] == ("oai-did=saved-device; session=fixture", "http://exit.test:80")
    assert get.call_args.args[2:4] == post.call_args.args[3:5]
    assert post.call_args.kwargs["extra_headers"]["OAI-Device-Id"] == "saved-device"
    assert get.call_args.kwargs["extra_headers"]["OAI-Device-Id"] == "saved-device"


def test_account_probe_reports_conflicting_device_cookie_without_checkout():
    account = {
        "access_token": "access-token",
        "registration_country": "IN",
        "identity_context": {"device_id": "saved-device"},
        "cookie_header": "oai-did=other-device; session=fixture",
    }
    with patch("sms_tool.accounts.account_payment_eligibility.payment_egress.assert_egress_countries"), \
         patch.object(gen_pp_link, "_checkout_post", side_effect=AssertionError("checkout must not start")):
        result = probe_account_payment_eligibility(account, proxy="http://exit.test:80")
    assert result["ok"] is False
    assert result["error_code"] == "checkout_identity_mismatch"
    assert result["methods"] == []
    assert "saved-device" not in str(result)
    assert "other-device" not in str(result)


def test_probe_never_echoes_credentials_back():
    def fake_probe(**_kwargs):
        return {"ok": True, "payment_method_types": ["card"]}

    account = {
        "email": "e@example.test",
        "access_token": "super-secret-at",
        "cookie_header": "super-secret-cookie",
    }
    with patch("sms_tool.payment_capability.payment_method_capability_probe", fake_probe):
        result = probe_account_payment_eligibility(account, proxy="http://user:pw@exit.test:80")

    blob = json.dumps(result)
    assert "super-secret-at" not in blob
    assert "super-secret-cookie" not in blob
    assert "pw@" not in blob


def test_probe_reports_failure_without_raising():
    def fake_probe(**_kwargs):
        return {
            "ok": False,
            "error": "checkout returned HTTP 403",
            "error_code": "checkout_unauthorized",
            "error_stage": "checkout_create",
            "retryable": True,
        }

    with patch("sms_tool.payment_capability.payment_method_capability_probe", fake_probe):
        result = probe_account_payment_eligibility({"access_token": "at"}, proxy="http://exit.test:80")

    assert result["ok"] is False
    assert result["error_code"] == "checkout_unauthorized"
    assert result["error_stage"] == "checkout_create"
    assert result["retryable"] is True
    assert result["methods"] == []


def test_probe_swallows_an_unexpected_exception():
    def fake_probe(**_kwargs):
        raise RuntimeError("boom")

    with patch("sms_tool.payment_capability.payment_method_capability_probe", fake_probe):
        result = probe_account_payment_eligibility({"access_token": "at"}, proxy="http://exit.test:80")

    assert result["ok"] is False
    assert result["error_code"] == "eligibility_probe_exception"


def test_probe_failure_without_an_error_string_still_names_the_reason():
    def fake_probe(**_kwargs):
        return {"ok": False, "decision": "payment_method_unavailable"}

    with patch("sms_tool.payment_capability.payment_method_capability_probe", fake_probe):
        result = probe_account_payment_eligibility({"access_token": "at"}, proxy="http://exit.test:80")

    assert result["ok"] is False
    assert result["error_code"] == "payment_method_unavailable"
    assert result["error_stage"] == "payment_eligibility"


# --------------------------------------------------------------------------
# Badge formatting
# --------------------------------------------------------------------------

def test_label_lists_methods_in_stripe_order():
    result = {"methods": ["card", "link", "apple_pay", "upi", "momo"]}
    assert payment_method_tokens(result) == ("card", "link", "apple_pay", "upi", "momo")
    assert payment_eligibility_label(result) == "card/link/apple_pay/upi/momo"


def test_label_caps_a_long_list_so_the_promotion_badge_stays_visible():
    result = {"methods": [f"m{index}" for index in range(12)]}
    assert payment_eligibility_label(result) == "m0/m1/m2/m3/m4/m5/m6/m7+4"


def test_label_is_empty_when_the_account_was_never_probed():
    """Never probed must stay blank -- it is not a failure.

    ``safe_snapshot()`` always emits the ``payment_capability`` key, so the
    empty mapping is what every untouched account looks like; marking it
    "unknown" would relabel the whole pool.
    """
    assert payment_eligibility_label({}) == ""
    assert payment_eligibility_label(None) == ""
    assert payment_eligibility_label("") == ""
    assert payment_eligibility_is_unknown({}) is False
    assert payment_eligibility_is_unknown(None) is False


def test_label_marks_a_probe_that_enumerated_nothing():
    """A populated record with no methods must read as unknown, not blank.

    Blank is indistinguishable from "never probed", and the live failure mode
    is a platform-side block (HTTP 400 on /backend-api/payments/checkout), so a
    silent blank invites reading a block as an account attribute.
    """
    assert payment_eligibility_label({"methods": []}) == PAYMENT_ELIGIBILITY_UNKNOWN_LABEL
    assert payment_eligibility_label({"ok": False, "error": "boom"}) == PAYMENT_ELIGIBILITY_UNKNOWN_LABEL
    assert (
        payment_eligibility_label(
            {
                "ok": False,
                "methods": [],
                "error_code": "checkout_failed",
                "error_stage": "checkout_create",
                "retryable": False,
            }
        )
        == PAYMENT_ELIGIBILITY_UNKNOWN_LABEL
    )
    assert payment_eligibility_is_unknown({"methods": []}) is True
    assert payment_eligibility_is_unknown({"ok": False, "error": "boom"}) is True
    # A record that DID enumerate methods is never unknown, even if a stale
    # ``ok`` flag disagrees.
    assert payment_eligibility_is_unknown({"ok": False, "methods": ["card"]}) is True
    assert payment_eligibility_label({"ok": False, "methods": ["card"]}) == PAYMENT_ELIGIBILITY_UNKNOWN_LABEL


def test_label_falls_back_to_the_ungrouped_lists():
    assert payment_eligibility_label({"ordered_payment_method_types": ["card"]}) == "card"
    assert payment_eligibility_label({"payment_method_types": ["card", "upi"]}) == "card/upi"


def test_composition_never_leaves_a_dangling_separator():
    assert promotion_status_with_eligibility("可试用Plus-100%", "card/upi/momo") == "可试用Plus-100% · card/upi/momo"
    assert promotion_status_with_eligibility("Free·无优惠", "") == "Free·无优惠"
    assert promotion_status_with_eligibility("", "card") == "card"
    assert promotion_status_with_eligibility("", "") == ""


def test_composition_shows_the_unknown_marker_next_to_the_promotion_label():
    assert (
        promotion_status_with_eligibility("Free·无优惠", PAYMENT_ELIGIBILITY_UNKNOWN_LABEL)
        == "Free·无优惠 · 支付资格未知"
    )
    assert (
        promotion_status_with_eligibility("可试用Plus-100%", PAYMENT_ELIGIBILITY_UNKNOWN_LABEL)
        == "可试用Plus-100% · 支付资格未知"
    )
    assert promotion_status_with_eligibility("", PAYMENT_ELIGIBILITY_UNKNOWN_LABEL) == "支付资格未知"


# --------------------------------------------------------------------------
# Persistence + the closed-whitelist guard
# --------------------------------------------------------------------------

def test_safe_snapshot_keeps_payment_capability():
    """The whitelist guard.

    ``store.accounts.upsert_account`` rebuilds raw_json from
    ``safe_snapshot()``; anything missing from that dict is dropped without a
    warning.  If this assertion ever fails, every relogin/health pass silently
    deletes the payment-eligibility result again.
    """
    model = AccountSessionModel.from_value({
        "email": "guard@example.test",
        "payment_capability": {"ok": True, "methods": ["card", "upi"]},
    })

    assert model.safe_snapshot()["payment_capability"] == {"ok": True, "methods": ["card", "upi"]}
    assert model.safe_snapshot()["payment_capability"] == dict(model.payment_capability)


def test_cli_promotion_defaults_to_plan_only_and_exposes_separate_probe():
    from sms_tool import cli

    parser = cli.build_parser()
    assert parser.parse_args(["--check-promotion"]).payment_eligibility is False
    assert parser.parse_args(["--check-promotion", "--payment-eligibility"]).payment_eligibility is True
    assert parser.parse_args(["--check-payment-eligibility"]).check_payment_eligibility is True


def test_mark_promotion_status_persists_and_survives_a_relogin_write(tmp_path):
    config = _config(tmp_path)
    session = {"email": "cap@example.test", "success": True, "access_token": "at"}
    assert upsert_account(session, runtime_config=config)

    assert mark_promotion_status(
        "cap@example.test",
        "Free·无优惠",
        payment_capability={"ok": True, "methods": ["card", "upi"]},
        runtime_config=config,
    )

    record = get_account_record("cap@example.test", runtime_config=config)
    stored = json.loads(record["raw_json"])
    assert stored["payment_capability"]["methods"] == ["card", "upi"]
    assert stored["payment_capability"]["updated_at"] > 0
    # The promotion label stays pure; the badge is composed at display time.
    assert stored["promotion_status"] == "Free·无优惠"

    # Relogin / account-health write: the payload is rebuilt from the stored
    # raw_json and re-serialized through safe_snapshot()'s whitelist.
    from sms_tool.accounts.account_recovery import _local_account_data

    relogin_payload = _local_account_data(record)
    relogin_payload["access_token"] = "replacement-at"
    assert upsert_account(relogin_payload, runtime_config=config)

    record = get_account_record("cap@example.test", runtime_config=config)
    assert json.loads(record["raw_json"])["payment_capability"]["methods"] == ["card", "upi"]


def test_empty_payment_capability_clears_a_stale_answer(tmp_path):
    """An empty dict means "the stored answer is no longer true", not "no data".

    A dead access token skips the eligibility probe, and pairing a fresh 401
    with a method list that was never re-verified is worse than showing nothing.
    """
    config = _config(tmp_path)
    assert upsert_account({"email": "stale@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "stale@example.test",
        "Free·无优惠",
        payment_capability={"ok": True, "methods": ["card"]},
        runtime_config=config,
    )

    assert mark_promotion_status(
        "stale@example.test",
        "AT失效",
        payment_capability={},
        runtime_config=config,
    )

    stored = json.loads(get_account_record("stale@example.test", runtime_config=config)["raw_json"])
    assert "payment_capability" not in stored


def test_none_leaves_a_previously_stored_answer_alone(tmp_path):
    """``None`` means "no probe ran" -- it must not wipe what is on disk."""
    config = _config(tmp_path)
    assert upsert_account({"email": "keep@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "keep@example.test",
        "Free·无优惠",
        payment_capability={"ok": True, "methods": ["card"]},
        runtime_config=config,
    )

    assert mark_promotion_status("keep@example.test", "Free·无优惠", runtime_config=config)

    stored = json.loads(get_account_record("keep@example.test", runtime_config=config)["raw_json"])
    assert stored["payment_capability"]["methods"] == ["card"]


def test_standalone_payment_save_preserves_promotion_marker(tmp_path):
    from sms_tool.storage import mark_payment_capability

    config = _config(tmp_path)
    assert upsert_account({"email": "standalone@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "standalone@example.test", "可试用Plus",
        promotion_result={"promotion_state": "trial_eligible"}, runtime_config=config,
    )
    previous = json.loads(get_account_record("standalone@example.test", runtime_config=config)["raw_json"])
    assert mark_payment_capability(
        "standalone@example.test", {"ok": True, "methods": ["upi"], "access_token": "secret"},
        runtime_config=config,
    )
    stored = json.loads(get_account_record("standalone@example.test", runtime_config=config)["raw_json"])
    assert stored["promotion_status"] == previous["promotion_status"]
    assert stored["promotion_state"] == previous["promotion_state"]
    assert stored["promotion_updated_at"] == previous["promotion_updated_at"]
    assert stored["payment_capability"]["methods"] == ["upi"]
    assert "access_token" not in stored["payment_capability"]


def test_standalone_payment_save_discards_unrecognized_and_nested_diagnostics(tmp_path):
    from sms_tool.storage import mark_payment_capability

    config = _config(tmp_path)
    assert upsert_account({"email": "private@example.test", "success": True}, runtime_config=config)
    assert mark_payment_capability(
        "private@example.test",
        {"ok": False, "methods": ["card"], "error_code": "checkout_risk_blocked",
         "error": "upstream secret", "extra": {"access_token": "secret"},
         "evidence_sources": ["stripe_init", "Bearer secret"]},
        runtime_config=config,
    )
    stored = json.loads(get_account_record("private@example.test", runtime_config=config)["raw_json"])
    capability = stored["payment_capability"]
    assert capability["error_code"] == "checkout_risk_blocked"
    assert capability["evidence_sources"] == ["stripe_init"]
    assert "upstream secret" not in json.dumps(capability)
    assert "secret" not in json.dumps(capability)


def test_currency_mismatch_never_displays_checkout_method_list():
    from sms_tool.accounts.account_payment_eligibility import _normalize_probe

    result = _normalize_probe({
        "ok": True, "decision": "checkout_currency_mismatch",
        "payment_method_types": ["card", "upi"], "currency": "USD",
    }, target_country="IN")
    assert result["ok"] is False
    assert result["methods"] == []
    assert result["error_code"] == "checkout_currency_mismatch"


def test_malformed_evidence_metadata_does_not_break_batch_normalization():
    from sms_tool.accounts.account_payment_eligibility import _normalize_probe

    result = _normalize_probe({
        "ok": True, "payment_method_types": ["card"],
        "checkout_kind": {"unexpected": True},
        "evidence_sources": [{"unexpected": True}, "stripe_init"],
    }, target_country="US")
    assert result["checkout_kind"] == ""
    assert result["evidence_sources"] == ["stripe_init"]


def test_normalized_failure_never_repeats_upstream_error_text():
    from sms_tool.accounts.account_payment_eligibility import _normalize_probe

    result = _normalize_probe({
        "ok": False, "error_code": "checkout_risk_blocked",
        "error_stage": "checkout_create", "error": "Bearer private-token",
        "payment_method_types": ["card"], "http_status": 400,
    }, target_country="US")
    assert result["error"] == "checkout_risk_blocked"
    assert result["methods"] == []
    assert "private-token" not in str(result)


def test_standalone_probe_uses_payment_exit_and_does_not_run_promotion(monkeypatch):
    from sms_tool.accounts import account_payment_eligibility

    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {
        "email": email, "access_token": "at", "raw_json": json.dumps({"registration_country": "IN"}),
    })
    monkeypatch.setattr("sms_tool.storage.mark_payment_capability", lambda *args, **kwargs: True)
    monkeypatch.setattr(account_payment_eligibility, "payment_proxy_pools", lambda config, method: {
        "checkout": ["http://in.example:80"],
    })
    observed = []
    monkeypatch.setattr(account_payment_eligibility, "probe_account_payment_eligibility",
                        lambda account, **kwargs: observed.append(kwargs["proxy"]) or {
                            "ok": True, "methods": ["upi"], "billing_country": "IN",
                        })
    result = account_payment_eligibility.probe_payment_eligibility_statuses(["e@example.test"])
    assert observed == ["http://in.example:80"]
    assert result["success"] == 1
    assert result["results"][0]["payment_eligibility"] == "upi"


def test_credential_keys_are_never_persisted_into_the_capability_blob(tmp_path):
    config = _config(tmp_path)
    assert upsert_account({"email": "scrub@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "scrub@example.test",
        "Free·无优惠",
        payment_capability={
            "ok": True,
            "methods": ["card"],
            "access_token": "leaked",
            "cookie_header": "leaked",
            "proxy": "http://user:pw@exit.test:80",
        },
        runtime_config=config,
    )

    blob = get_account_record("scrub@example.test", runtime_config=config)["raw_json"]
    assert "leaked" not in blob
    assert "pw@" not in blob
    assert json.loads(blob)["payment_capability"]["methods"] == ["card"]


# --------------------------------------------------------------------------
# Desktop read composition
# --------------------------------------------------------------------------

def test_desktop_read_composes_the_promotion_and_eligibility_badges(tmp_path):
    from sms_tool.desktop_read import read_account

    config = _config(tmp_path)
    assert upsert_account({"email": "show@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "show@example.test",
        "可试用Plus-100%",
        promotion_state="trial_eligible",
        payment_capability={"ok": True, "methods": ["card", "upi", "momo"]},
        runtime_config=config,
    )

    payload = read_account(email="show@example.test", runtime_config=config)

    assert payload["promotion_status"] == "可试用Plus-100%"
    assert payload["promotion_state"] == "trial_eligible"
    assert payload["payment_eligibility"] == "card/upi/momo"
    assert payload["promotion_display"] == "可试用Plus-100% · card/upi/momo"


def test_desktop_read_leaves_promotion_display_alone_without_eligibility(tmp_path):
    from sms_tool.desktop_read import read_account

    config = _config(tmp_path)
    assert upsert_account({"email": "plain@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "plain@example.test",
        "Free·无优惠",
        promotion_state="free",
        runtime_config=config,
    )

    payload = read_account(email="plain@example.test", runtime_config=config)

    assert payload["promotion_display"] == "Free·无优惠"
    assert "payment_eligibility" not in payload


def test_desktop_read_marks_a_failed_probe_instead_of_leaving_it_blank(tmp_path):
    """The 优惠状态 column must say "unknown", not look unprobed.

    Live shape of the failure (2026-09-21/22): the promotion probe returns 200
    while the eligibility probe is rejected by platform risk control with
    HTTP 400 on /backend-api/payments/checkout, so ``methods`` is empty.  A
    blank suffix here would read as "this account has no payment rails".
    """
    from sms_tool.desktop_read import read_account

    config = _config(tmp_path)
    assert upsert_account({"email": "blocked@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "blocked@example.test",
        "Free·无优惠",
        promotion_state="free",
        payment_capability={
            "ok": False,
            "methods": [],
            "billing_country": "IN",
            "carrier_method": "direct_card",
            "error_code": "checkout_failed",
            "error_stage": "checkout_create",
            "retryable": False,
        },
        runtime_config=config,
    )

    payload = read_account(email="blocked@example.test", runtime_config=config)

    assert payload["promotion_status"] == "Free·无优惠"
    assert payload["payment_eligibility"] == PAYMENT_ELIGIBILITY_UNKNOWN_LABEL
    assert payload["promotion_display"] == "Free·无优惠 · 支付资格未知"


def test_desktop_read_does_not_mark_an_account_the_probe_never_reached(tmp_path):
    """The empty mapping is the untouched-account shape, not a failure.

    ``AccountSessionModel.safe_snapshot()`` always emits ``payment_capability``
    and every account that has not been probed carries ``{}``.  Treating that
    as "unknown" would relabel the whole pool on the next relogin pass.
    """
    from sms_tool.desktop_read import read_account

    config = _config(tmp_path)
    assert upsert_account({"email": "untouched@example.test", "success": True, "access_token": "at"}, runtime_config=config)
    assert mark_promotion_status(
        "untouched@example.test",
        "Free·无优惠",
        promotion_state="free",
        runtime_config=config,
    )

    payload = read_account(email="untouched@example.test", runtime_config=config)

    assert payload["promotion_display"] == "Free·无优惠"
    assert "payment_eligibility" not in payload


@pytest.mark.parametrize("status_code", ["", "0", "500"])
def test_a_non_401_promotion_failure_does_not_start_checkout(status_code, monkeypatch):
    from sms_tool.accounts import promotion_batch

    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        promotion_batch,
        "check_account_promotion",
        lambda account, **kwargs: {
            "ok": False, "status_code": status_code, "promotion_status": "检测失败", "error": "http_500"
        },
    )
    with patch.object(promotion_batch, "_probe_payment_eligibility") as eligibility:
        result = promotion_batch.refresh_promotion_statuses(["fail@example.test"], workers=1)

    eligibility.assert_not_called()
    assert result["payment_eligibility_ok"] == 0
    assert result["payment_eligibility_failed"] == 0


def test_a_401_promotion_failure_skips_the_probe():
    from sms_tool.accounts.promotion_batch import _promotion_probe_is_unauthorized

    assert _promotion_probe_is_unauthorized({"ok": False, "status_code": 401}) is True
    assert _promotion_probe_is_unauthorized({"ok": False, "promotion_state": "auth_invalid"}) is True
    assert _promotion_probe_is_unauthorized({"ok": True, "status_code": 200}) is False


def test_a_401_promotion_failure_clears_stale_payment_methods(monkeypatch):
    from sms_tool.accounts import promotion_batch

    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    saved = []
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: saved.append(kwargs) or True)
    monkeypatch.setattr(
        promotion_batch, "check_account_promotion",
        lambda account, **kwargs: {"ok": False, "status_code": 401, "promotion_status": "AT失效"},
    )
    with patch.object(promotion_batch, "_probe_payment_eligibility") as eligibility:
        promotion_batch.refresh_promotion_statuses(["dead@example.test"], workers=1)

    eligibility.assert_not_called()
    assert saved[0]["payment_capability"] == {}


def test_a_successful_promotion_passes_its_exit_to_eligibility(monkeypatch):
    from sms_tool.accounts import promotion_batch

    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        promotion_batch, "check_account_promotion",
        lambda account, **kwargs: {"ok": True, "promotion_status": "Free·无优惠"},
    )
    with patch.object(
        promotion_batch, "_probe_payment_eligibility",
        return_value={"ok": True, "methods": ["card"]},
    ) as eligibility:
        result = promotion_batch.refresh_promotion_statuses(
            ["ok@example.test"], workers=1, proxy="http://exit.test:80", payment_eligibility=True,
        )

    assert eligibility.call_args.kwargs["proxy"] == "http://exit.test:80"
    assert result["payment_eligibility_ok"] == 1


def test_successful_promotion_does_not_implicitly_create_checkout(monkeypatch):
    from sms_tool.accounts import promotion_batch

    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {"email": email, "access_token": "at"})
    monkeypatch.setattr("sms_tool.storage.mark_promotion_status", lambda *args, **kwargs: True)
    monkeypatch.setattr(promotion_batch, "check_account_promotion",
                        lambda account, **kwargs: {"ok": True, "promotion_status": "Free·无优惠"})
    with patch.object(promotion_batch, "_probe_payment_eligibility") as checkout:
        result = promotion_batch.refresh_promotion_statuses(["e@example.test"], proxy="http://exit.test:80")
    checkout.assert_not_called()
    assert result["payment_eligibility_ok"] == 0


def test_eligibility_rejects_a_mismatched_exit_before_checkout():
    from sms_tool.payment_egress import EgressCheckError

    error = EgressCheckError(
        "wrong exit", error_code="egress_country_mismatch", retryable=True,
        stage="checkout", expected_country="IN", observed_country="US",
    )
    with patch("sms_tool.payment_egress.assert_egress_countries", side_effect=error) as gate, patch(
        "sms_tool.payment_capability.payment_method_capability_probe"
    ) as checkout:
        result = probe_account_payment_eligibility(
            {"access_token": "at", "registration_country": "IN"},
            proxy="http://user:secret@exit.test:80",
        )

    gate.assert_called_once()
    assert gate.call_args.args[0]["stage_proxy_countries"]["checkout"] == "IN"
    checkout.assert_not_called()
    assert result["ok"] is False
    assert result["error_code"] == "egress_country_mismatch"
    assert result["observed_country"] == "US"
    assert "secret" not in json.dumps(result)


def test_eligibility_retargets_proxy_region_for_account_before_egress_check():
    original = "http://demo-region-VN-sid-Fixture123-t-5:sample@proxy.example:2000"
    with patch("sms_tool.payment_egress.assert_egress_countries") as gate, patch(
        "sms_tool.payment_capability.payment_method_capability_probe",
        return_value={"ok": True, "payment_method_types": ["upi"], "currency": "INR"},
    ) as checkout:
        result = probe_account_payment_eligibility(
            {"access_token": "at", "registration_country": "IN"}, proxy=original,
        )

    routed = gate.call_args.args[0]["checkout_proxy"]
    assert routed == original.replace("region-VN", "region-IN")
    assert checkout.call_args.kwargs["proxy"] == routed
    assert result["ok"] is True
    assert original.endswith("proxy.example:2000")
    assert "sample" not in json.dumps(result)


def test_eligibility_retargeted_region_still_requires_verified_exit():
    from sms_tool.payment_egress import EgressCheckError

    error = EgressCheckError(
        "wrong exit", error_code="egress_country_mismatch", retryable=True,
        stage="checkout", expected_country="IN", observed_country="VN",
    )
    with patch("sms_tool.payment_egress.assert_egress_countries", side_effect=error) as gate, patch(
        "sms_tool.payment_capability.payment_method_capability_probe"
    ) as checkout:
        result = probe_account_payment_eligibility(
            {"access_token": "at", "registration_country": "IN"},
            proxy="http://demo-region-VN-sid-Fixture123-t-5:sample@proxy.example:2000",
        )

    assert "region-IN" in gate.call_args.args[0]["checkout_proxy"]
    checkout.assert_not_called()
    assert result["error_code"] == "egress_country_mismatch"
    assert result["observed_country"] == "VN"


def test_standalone_batch_uses_each_accounts_country_for_the_same_proxy_template(monkeypatch):
    from sms_tool.accounts import account_payment_eligibility as eligibility

    original = "http://demo-region-VN-sid-Fixture123-t-5:sample@proxy.example:2000"
    countries = {"first@example.test": "IN", "second@example.test": "VN"}
    monkeypatch.setattr("sms_tool.storage.get_account_record", lambda email: {
        "access_token": "at", "raw_json": json.dumps({"registration_country": countries[email]}),
    })
    monkeypatch.setattr("sms_tool.storage.mark_payment_capability", lambda *args, **kwargs: True)
    monkeypatch.setattr(eligibility, "payment_proxy_pools", lambda config, method: {"checkout": []})
    exits = []

    def gate(options, **_kwargs):
        exits.append(options["checkout_proxy"])

    def checkout(**kwargs):
        return {"ok": True, "currency": kwargs["currency"], "payment_method_types": ["card"]}

    with patch("sms_tool.payment_egress.assert_egress_countries", side_effect=gate), patch(
        "sms_tool.payment_capability.payment_method_capability_probe", side_effect=checkout,
    ):
        report = eligibility.probe_payment_eligibility_statuses(
            list(countries), proxy=original,
        )

    assert report["success"] == 2
    assert exits == [original.replace("region-VN", "region-IN"), original]
    assert "sample" not in json.dumps(report)


def test_eligibility_rejects_an_unverifiable_exit_before_checkout():
    with patch("sms_tool.payment_egress.assert_egress_countries") as gate, patch(
        "sms_tool.payment_capability.payment_method_capability_probe"
    ) as checkout:
        result = probe_account_payment_eligibility({"access_token": "at", "registration_country": "IN"})

    gate.assert_not_called()
    checkout.assert_not_called()
    assert result["ok"] is False
    assert result["error_code"] == "egress_country_unverified"


def test_eligibility_does_not_expose_unexpected_egress_errors():
    with patch("sms_tool.payment_egress.assert_egress_countries", side_effect=RuntimeError("Bearer secret-token")), patch(
        "sms_tool.payment_capability.payment_method_capability_probe"
    ) as checkout:
        result = probe_account_payment_eligibility(
            {"access_token": "at", "registration_country": "IN"}, proxy="http://exit.test:80"
        )

    checkout.assert_not_called()
    assert result["error_code"] == "egress_probe_failed"
    assert "secret-token" not in json.dumps(result)


def test_eligibility_proceeds_after_verified_matching_exit():
    with patch("sms_tool.payment_egress.assert_egress_countries") as gate, patch(
        "sms_tool.payment_capability.payment_method_capability_probe",
        return_value={"ok": True, "payment_method_types": ["upi"]},
    ) as checkout:
        result = probe_account_payment_eligibility(
            {"access_token": "at", "registration_country": "IN"},
            proxy="http://exit.test:80",
        )

    gate.assert_called_once()
    checkout.assert_called_once()
    assert result["methods"] == ["upi"]
    assert result["billing_country"] == "IN"
