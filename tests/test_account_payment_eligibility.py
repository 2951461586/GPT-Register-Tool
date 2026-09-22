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
from unittest.mock import patch

import pytest

from sms_tool.accounts.account_models import AccountSessionModel
from sms_tool.accounts.account_payment_eligibility import (
    billing_country_for,
    billing_currency_for,
    payment_locale_for,
    probe_account_payment_eligibility,
)
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
    # These live in the local extras because paypal_extract.CURRENCY_MAP
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
        result = probe_account_payment_eligibility({"access_token": "at"})

    assert result["ok"] is False
    assert result["error_code"] == "checkout_unauthorized"
    assert result["error_stage"] == "checkout_create"
    assert result["retryable"] is True
    assert result["methods"] == []


def test_probe_swallows_an_unexpected_exception():
    def fake_probe(**_kwargs):
        raise RuntimeError("boom")

    with patch("sms_tool.payment_capability.payment_method_capability_probe", fake_probe):
        result = probe_account_payment_eligibility({"access_token": "at"})

    assert result["ok"] is False
    assert result["error_code"] == "eligibility_probe_exception"


def test_probe_failure_without_an_error_string_still_names_the_reason():
    def fake_probe(**_kwargs):
        return {"ok": False, "decision": "payment_method_unavailable"}

    with patch("sms_tool.payment_capability.payment_method_capability_probe", fake_probe):
        result = probe_account_payment_eligibility({"access_token": "at"})

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
    assert payment_eligibility_is_unknown({"ok": False, "methods": ["card"]}) is False


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
def test_a_non_401_promotion_failure_still_probes_eligibility(status_code):
    """Only a proven-dead token skips the probe; a 500 does not prove anything."""
    from sms_tool.accounts.account_promotion import _promotion_probe_is_unauthorized

    assert _promotion_probe_is_unauthorized({"ok": False, "status_code": status_code}) is False


def test_a_401_promotion_failure_skips_the_probe():
    from sms_tool.accounts.account_promotion import _promotion_probe_is_unauthorized

    assert _promotion_probe_is_unauthorized({"ok": False, "status_code": 401}) is True
    assert _promotion_probe_is_unauthorized({"ok": False, "promotion_state": "auth_invalid"}) is True
    assert _promotion_probe_is_unauthorized({"ok": True, "status_code": 200}) is False
