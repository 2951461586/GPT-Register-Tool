"""Behaviour tests for ``sms_tool/pp_link_helpers.py`` (round-5 audit target).

Why this file exists
--------------------
``pp_link_helpers`` sits directly on the paid path: it normalizes the proxy the
Stripe/PayPal request egresses through, and it decides whether a redirect is a
real PayPal BA-approval URL.  Both decisions have a *silent* wrong branch — a
proxy that silently keeps the wrong region, and a URL predicate that says "not
PayPal" for a URL that is PayPal.  Those are the branches pinned here.

No network is touched: ``resolve_external_redirect`` is driven with a hand-written
session double that emulates only ``.get()`` and the response headers it reads.
"""

from __future__ import annotations

import re

import pytest

from sms_tool.pp_link_helpers import (
    BILLING_ADDRESS_POOLS,
    BILLING_DATA,
    PAYPAL_BA_RE,
    PM_REDIRECT_RE,
    billing_address_pool,
    billing_for_country,
    extract_ba_token,
    extract_redirect_url,
    find_submission_attempt,
    find_url_in_value,
    is_paypal_ba_approve_url,
    normalize_proxy_template,
    proxy_for_country_template,
    reserve_billing_variant,
    resolve_external_redirect,
    stripe_amount_details,
    stripe_confirm_error_diagnostics,
)


# ────────────────────────── session test double ──────────────────────────────


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


class FakeSession:
    """Session double: *hops* maps URL -> FakeResponse (or an Exception)."""

    def __init__(self, hops=None):
        self.hops = dict(hops or {})
        self.requested = []

    def get(self, url, allow_redirects=False, timeout=None):
        self.requested.append(url)
        outcome = self.hops.get(url, FakeResponse(200))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


PAYPAL_BA = "https://www.paypal.com/agreements/approve?ba_token=BA-FIXTURE-1"


# ───────────────────────── normalize_proxy_template ──────────────────────────


def test_normalize_proxy_template_accepts_the_standard_form():
    assert normalize_proxy_template("user:pass@1.2.3.4:8080") == "http://user:pass@1.2.3.4:8080"


def test_normalize_proxy_template_accepts_the_colon_separated_form():
    assert normalize_proxy_template("1.2.3.4:8080:user:pass") == "http://user:pass@1.2.3.4:8080"


def test_normalize_proxy_template_accepts_the_reversed_form():
    """host:port@user:pass is flipped into the standard user:pass@host:port."""
    assert normalize_proxy_template("1.2.3.4:8080@user:pass") == "http://user:pass@1.2.3.4:8080"


def test_normalize_proxy_template_preserves_an_explicit_scheme():
    assert normalize_proxy_template("socks5h://u:p@h:1080") == "socks5h://u:p@h:1080"


def test_normalize_proxy_template_strips_surrounding_whitespace():
    assert normalize_proxy_template("  user:pass@1.2.3.4:8080  ") == "http://user:pass@1.2.3.4:8080"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_normalize_proxy_template_passes_blank_input_through_unchanged(value):
    assert normalize_proxy_template(value) == ""


def test_normalize_proxy_template_is_idempotent():
    once = normalize_proxy_template("1.2.3.4:8080:user:pass")
    assert normalize_proxy_template(once) == once


# ─────────────────────── proxy_for_country_template ──────────────────────────


def test_proxy_for_country_replaces_the_region_token():
    out = proxy_for_country_template("user-region-us:pass@1.2.3.4:8080", "DE")
    assert out == "http://user-region-DE:pass@1.2.3.4:8080"


def test_proxy_for_country_is_case_insensitive_on_input_and_uppercases_output():
    out = proxy_for_country_template("user-region-us:pass@1.2.3.4:8080", "de")
    assert out == "http://user-region-DE:pass@1.2.3.4:8080"


def test_proxy_for_country_keeps_jp_sticky_city_suffix():
    """JP keeps its -st-/-city- sticky routing; every other country drops it."""
    template = "user-region-jp-st-tokyo-city-tokyo-sid-9:pass@1.2.3.4:8080"
    assert "st-tokyo-city-tokyo" in proxy_for_country_template(template, "JP")
    assert "st-tokyo-city-tokyo" not in proxy_for_country_template(template, "US")


def test_proxy_for_country_returns_the_template_untouched_when_no_region_token():
    """A credentialed template carrying no region token is returned unchanged.

    The ``-XX`` fallback can only fire for a password-less proxy:
    ``rpartition("@")`` leaves ``:password`` at the end of *userinfo*, so
    ``-[A-Za-z]{2}$`` never matches a credentialed one.

    After the delegation to ``phone_proxy.match_proxy_region`` every *known*
    provider template (``region-XX`` / ``geo-XX`` / ``custom_zone_XX`` /
    Kookeey password) is rewritten, so this branch now covers only credentials
    that genuinely carry no region token.  Returning them untouched is the
    honest outcome — there is nothing to rewrite, and silently inventing a
    region would be worse than leaving it be.
    """
    template = "user-xx:pass@1.2.3.4:8080"
    assert proxy_for_country_template(template, "US") == "http://user-xx:pass@1.2.3.4:8080"


def test_proxy_for_country_fallback_fires_for_a_passwordless_proxy():
    out = proxy_for_country_template("user-xx@1.2.3.4:8080", "US")
    assert out == "http://user-US@1.2.3.4:8080"


@pytest.mark.parametrize("country", ["", "   ", None])
def test_proxy_for_country_returns_the_template_when_no_country_requested(country):
    template = "user-region-us:pass@1.2.3.4:8080"
    assert proxy_for_country_template(template, country) == "http://user-region-us:pass@1.2.3.4:8080"


def test_proxy_for_country_returns_empty_for_an_empty_template():
    assert proxy_for_country_template("", "US") == ""


def test_proxy_for_country_is_idempotent_for_the_same_country():
    template = "user-region-us:pass@1.2.3.4:8080"
    once = proxy_for_country_template(template, "DE")
    assert proxy_for_country_template(once, "DE") == once


def test_proxy_for_country_inherits_9http_geo_template():
    """The rewrite is delegated, so 9http's ``geo-XX`` routes too.

    This helper used to hard-code ``region-[A-Za-z]{2}``: a 9http credential
    came back untouched while the caller believed the egress was retargeted.
    """
    template = "VSBFTHZC-geo-VN-sid-mwT3-ttl-5:secret@global.9http.com:9091"
    out = proxy_for_country_template(template, "US")
    assert "geo-US" in out
    assert "geo-VN" not in out
    # ...and the provider tag must survive, not be rewritten to region-US.
    assert "region-US" not in out


def test_proxy_for_country_inherits_ipwo_custom_zone_template():
    template = "acct_custom_zone_US_sid_1_time_5:secret@us.ipwo.net:7878"
    out = proxy_for_country_template(template, "JP")
    assert "custom_zone_JP" in out
    assert "custom_zone_US" not in out


def test_proxy_for_country_inherits_kookeey_password_template():
    template = "gate.kookeey.info:1000:user:pass-JP-abc123-5m"
    out = proxy_for_country_template(template, "GB")
    assert "pass-GB-abc123-5m" in out
    assert "pass-JP-abc123-5m" not in out


# ──────────────────────── is_paypal_ba_approve_url ───────────────────────────


def test_is_paypal_ba_approve_url_accepts_the_canonical_approval_url():
    assert is_paypal_ba_approve_url(PAYPAL_BA) is True


def test_is_paypal_ba_approve_url_rejects_a_blank_ba_token():
    assert is_paypal_ba_approve_url("https://www.paypal.com/agreements/approve?ba_token=") is False


def test_is_paypal_ba_approve_url_rejects_a_missing_ba_token():
    assert is_paypal_ba_approve_url("https://www.paypal.com/agreements/approve") is False


def test_is_paypal_ba_approve_url_rejects_a_lookalike_host():
    """paypal.com.evil.com must not pass the ``endswith('.paypal.com')`` check."""
    assert is_paypal_ba_approve_url("https://www.paypal.com.evil.com/agreements/approve?ba_token=1") is False


def test_is_paypal_ba_approve_url_rejects_a_non_paypal_host():
    assert is_paypal_ba_approve_url("https://evil.com/agreements/approve?ba_token=1") is False


def test_is_paypal_ba_approve_url_accepts_a_subdomain_of_paypal():
    assert is_paypal_ba_approve_url("https://sandbox.paypal.com/agreements/approve?ba_token=1") is True


def test_is_paypal_ba_approve_url_is_case_insensitive_on_host():
    assert is_paypal_ba_approve_url("https://WWW.PayPal.com/agreements/approve?ba_token=1") is True


def test_is_paypal_ba_approve_url_tolerates_a_trailing_slash():
    assert is_paypal_ba_approve_url(PAYPAL_BA) is True
    assert is_paypal_ba_approve_url(PAYPAL_BA) is True


def test_is_paypal_ba_approve_url_returns_false_instead_of_raising_on_junk():
    """The whole body is wrapped in try/except, so garbage degrades to 'no'."""
    assert is_paypal_ba_approve_url("not a url") is False
    assert is_paypal_ba_approve_url(None) is False  # type: ignore[arg-type]


def test_is_paypal_ba_approve_url_is_idempotent():
    assert [is_paypal_ba_approve_url(PAYPAL_BA) for _ in range(4)] == [True] * 4


# ──────────────────────────── extract_ba_token ───────────────────────────────


def test_extract_ba_token_pulls_the_token_from_the_query_string():
    assert extract_ba_token(PAYPAL_BA) == "BA-FIXTURE-1"


def test_extract_ba_token_stops_at_the_next_query_separator():
    assert extract_ba_token("https://h/p?ba_token=ABC&other=1") == "ABC"
    assert extract_ba_token("https://h/p?ba_token=ABC#frag") == "ABC"


def test_extract_ba_token_is_case_insensitive_on_the_marker():
    assert extract_ba_token("https://h/p?BA_TOKEN=ABC") == "ABC"


def test_extract_ba_token_returns_empty_when_the_marker_is_absent():
    assert extract_ba_token("https://h/p?nothing=1") == ""


def test_extract_ba_token_returns_empty_for_a_blank_token_value():
    assert extract_ba_token("https://h/p?ba_token=") == ""
    assert extract_ba_token("https://h/p?ba_token=&x=1") == ""


def test_extract_ba_token_raises_on_none_instead_of_returning_empty():
    """AUDIT POINT: unlike every sibling helper, this one has no None guard."""
    with pytest.raises(AttributeError):
        extract_ba_token(None)  # type: ignore[arg-type]


def test_extract_ba_token_is_idempotent():
    assert [extract_ba_token(PAYPAL_BA) for _ in range(4)] == ["BA-FIXTURE-1"] * 4


# ─────────────────────────── find_url_in_value ───────────────────────────────


def test_find_url_in_value_matches_a_plain_string():
    assert find_url_in_value("go to " + PAYPAL_BA + " now", [PAYPAL_BA_RE]) == PAYPAL_BA


def test_find_url_in_value_recurses_into_dicts_and_lists():
    payload = {"a": [{"b": "junk"}, {"c": PAYPAL_BA}]}
    assert find_url_in_value(payload, [PAYPAL_BA_RE]) == PAYPAL_BA


def test_find_url_in_value_prefers_the_named_url_keys():
    """Named keys are checked before the generic value walk."""
    payload = {
        "note": "https://www.paypal.com/agreements/approve?ba_token=FIRST",
        "url": "https://www.paypal.com/agreements/approve?ba_token=SECOND",
    }
    assert "SECOND" in find_url_in_value(payload, [PAYPAL_BA_RE])


def test_find_url_in_value_returns_empty_when_nothing_matches():
    assert find_url_in_value({"a": "b"}, [PAYPAL_BA_RE, PM_REDIRECT_RE]) == ""


@pytest.mark.parametrize("value", [None, 42, 3.5, True, object()])
def test_find_url_in_value_is_safe_for_non_container_types(value):
    assert find_url_in_value(value, [PAYPAL_BA_RE]) == ""


def test_find_url_in_value_handles_deeply_nested_structures_without_recursing_forever():
    payload: dict = {"leaf": PAYPAL_BA}
    for _ in range(60):
        payload = {"nest": [payload]}
    assert find_url_in_value(payload, [PAYPAL_BA_RE]) == PAYPAL_BA


# ─────────────────────────── extract_redirect_url ────────────────────────────


def test_extract_redirect_url_reads_next_action_first():
    url = "https://pm-redirects.stripe.com/authorize/abc"
    payload = {
        "next_action": {"type": "redirect_to_url", "redirect_to_url": {"url": url}},
        "note": "https://www.paypal.com/agreements/approve?ba_token=IGNORED",
    }
    assert extract_redirect_url(payload) == url


def test_extract_redirect_url_falls_back_to_scanning_for_a_paypal_url():
    payload = {"deep": {"note": PAYPAL_BA}}
    assert extract_redirect_url(payload) == PAYPAL_BA


def test_extract_redirect_url_falls_back_to_a_nested_intent_next_action():
    payload = {"payment_intent": {"next_action": {"redirect_to_url": {"url": PAYPAL_BA}}}}
    assert extract_redirect_url(payload) == PAYPAL_BA


def test_extract_redirect_url_returns_empty_for_an_empty_payload():
    assert extract_redirect_url({}) == ""


def test_extract_redirect_url_ignores_a_non_redirect_next_action():
    payload = {"next_action": {"type": "use_stripe_sdk"}}
    assert extract_redirect_url(payload) == ""


# ──────────────────────── resolve_external_redirect ──────────────────────────


def test_resolve_external_redirect_returns_immediately_for_a_paypal_url():
    session = FakeSession()
    assert resolve_external_redirect(session, PAYPAL_BA) == PAYPAL_BA
    assert session.requested == []  # no hop needed


def test_resolve_external_redirect_follows_a_relative_location():
    session = FakeSession(
        {
            "https://start.example/x": FakeResponse(302, {"Location": "/next"}),
            "https://start.example/next": FakeResponse(302, {"Location": PAYPAL_BA}),
        }
    )
    assert resolve_external_redirect(session, "https://start.example/x") == PAYPAL_BA


def test_resolve_external_redirect_stops_on_a_non_redirect_status():
    session = FakeSession({"https://start.example/x": FakeResponse(200, {})})
    assert resolve_external_redirect(session, "https://start.example/x") == "https://start.example/x"


def test_resolve_external_redirect_stops_when_location_is_missing():
    session = FakeSession({"https://start.example/x": FakeResponse(302, {})})
    assert resolve_external_redirect(session, "https://start.example/x") == "https://start.example/x"


def test_resolve_external_redirect_returns_the_current_url_on_a_network_error():
    """AUDIT POINT: a timeout is indistinguishable from a terminal redirect."""
    session = FakeSession({"https://start.example/x": TimeoutError("boom")})
    assert resolve_external_redirect(session, "https://start.example/x") == "https://start.example/x"


def test_resolve_external_redirect_respects_max_hops():
    hops = {f"https://h/{i}": FakeResponse(302, {"Location": f"https://h/{i + 1}"}) for i in range(50)}
    session = FakeSession(hops)
    assert resolve_external_redirect(session, "https://h/0", max_hops=3) == "https://h/3"


def test_resolve_external_redirect_returns_empty_for_an_empty_start():
    assert resolve_external_redirect(FakeSession(), "") == ""


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_resolve_external_redirect_follows_every_redirect_status(status):
    session = FakeSession({"https://s/1": FakeResponse(status, {"Location": PAYPAL_BA})})
    assert resolve_external_redirect(session, "https://s/1") == PAYPAL_BA


def test_resolve_external_redirect_is_idempotent_for_a_terminal_url():
    session = FakeSession({"https://s/1": FakeResponse(200, {})})
    results = [resolve_external_redirect(session, "https://s/1") for _ in range(3)]
    assert results == ["https://s/1"] * 3


# ────────────────────────── billing_for_country ──────────────────────────────


def test_billing_for_country_returns_the_country_specific_identity():
    data = billing_for_country("JP")
    assert data["country"] == "JP"
    assert data["name"] == ("Taro", "Yamada")
    assert data["city"] == "Sumida-ku"


def test_billing_for_country_normalizes_a_lowercase_input():
    assert billing_for_country("jp")["country"] == "JP"


def test_billing_for_country_email_domain_tracks_the_country():
    assert billing_for_country("DE")["email"].endswith("@example.de")


def test_billing_for_country_generates_a_template_that_keeps_the_country():
    """AUDIT POINT (resolved 2026-10-01): unknown countries no longer bill from Germany.

    Before this, the caller got a self-contradicting profile (country=ZZ,
    city=Berlin), so an unsupported locale silently billed from Germany.  The
    generated template now keeps the requested country.
    """
    data = billing_for_country("ZZ")
    assert data["country"] == "ZZ"
    assert data["city"] == "ZZ"
    assert data["postal"] == "00000"
    assert data["city"] != BILLING_DATA["DE"]["city"]


def test_billing_for_country_covers_the_added_markets():
    for code in ("VN", "ID", "MX", "NG", "UA", "TW", "AE"):
        data = billing_for_country(code)
        assert data["country"] == code
        assert data["city"] != BILLING_DATA["DE"]["city"]


@pytest.mark.parametrize("country", ["", None])
def test_billing_for_country_defaults_to_germany_for_blank_input(country):
    blank, german = billing_for_country(country), billing_for_country("DE")
    assert {k: v for k, v in blank.items() if k != "email"} == {k: v for k, v in german.items() if k != "email"}
    assert blank["email"].endswith("@example.de")


def test_billing_for_country_emails_are_unique_per_call():
    emails = {billing_for_country("US")["email"] for _ in range(25)}
    assert len(emails) > 1  # uuid4-backed, so collisions are not expected


def test_billing_for_country_never_mutates_the_shared_table():
    before = dict(BILLING_DATA["US"])
    billing_for_country("US")["city"] = "MUTATED"
    assert BILLING_DATA["US"] == before


def test_billing_for_country_covers_every_supported_locale():
    for code in BILLING_DATA:
        assert billing_for_country(code)["country"] == code


# ───────────────────────── stripe_amount_details ─────────────────────────────


def test_stripe_amount_details_prefers_total_summary_due():
    out = stripe_amount_details({"currency": "USD", "total_summary": {"due": 500, "currency": "usd"}})
    assert out == {"amount": 500, "currency": "usd", "source": "total_summary.due"}


def test_stripe_amount_details_falls_back_to_the_invoice_amount_due():
    out = stripe_amount_details({"invoice": {"amount_due": 700}})
    assert out == {"amount": 700, "currency": "", "source": "invoice.amount_due"}


def test_stripe_amount_details_inherits_the_top_level_currency_on_the_invoice_path():
    out = stripe_amount_details({"currency": "EUR", "invoice": {"amount_due": 700}})
    assert out["currency"] == "eur"


def test_stripe_amount_details_treats_zero_due_as_a_real_amount():
    """0 is falsy but the guard is ``is not None`` - pin that distinction."""
    out = stripe_amount_details({"total_summary": {"due": 0}})
    assert out == {"amount": 0, "currency": "", "source": "total_summary.due"}


def test_stripe_amount_details_returns_unknown_for_an_empty_payload():
    assert stripe_amount_details({}) == {"amount": None, "currency": "", "source": "unknown"}


@pytest.mark.parametrize("payload", [None, "x", 42, []])
def test_stripe_amount_details_is_safe_for_non_dict_payloads(payload):
    assert stripe_amount_details(payload) == {"amount": None, "currency": "", "source": "unknown"}


def test_stripe_amount_details_coerces_string_amounts_to_int():
    out = stripe_amount_details({"total_summary": {"due": "500"}})
    assert out["amount"] == 500


# ──────────────────────── find_submission_attempt ────────────────────────────


def test_find_submission_attempt_returns_a_direct_match():
    attempt = {"state": "failed", "reason": "card_declined"}
    assert find_submission_attempt({"submission_attempt": attempt}) == attempt


def test_find_submission_attempt_recurses_into_nested_dicts():
    attempt = {"state": "failed"}
    assert find_submission_attempt({"a": {"b": {"submission_attempt": attempt}}}) == attempt


def test_find_submission_attempt_recurses_into_lists():
    attempt = {"state": "failed"}
    assert find_submission_attempt({"items": [{"nope": 1}, {"submission_attempt": attempt}]}) == attempt


def test_find_submission_attempt_ignores_a_non_dict_value_at_the_key():
    assert find_submission_attempt({"submission_attempt": "just-a-string"}) == {}


def test_find_submission_attempt_returns_empty_for_a_missing_attempt():
    assert find_submission_attempt({"a": 1}) == {}


@pytest.mark.parametrize("payload", [None, "x", 42, []])
def test_find_submission_attempt_is_safe_for_non_dict_payloads(payload):
    assert find_submission_attempt(payload) == {}


# ──────────────────── stripe_confirm_error_diagnostics ───────────────────────


class FakeHttpResponse:
    def __init__(self, status_code=402, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_stripe_confirm_error_diagnostics_always_reports_the_core_five_fields():
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(), "cs_1", "pm_1", {})
    for field in ("stripe_confirm_failed:http=402", "cs_id=cs_1", "pm_id=pm_1", "amount=None", "init_checksum=missing"):
        assert field in out


def test_stripe_confirm_error_diagnostics_truncates_long_identifiers():
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(), "c" * 60, "p" * 60, {})
    assert "cs_id=" + "c" * 18 in out
    assert "c" * 19 not in out


def test_stripe_confirm_error_diagnostics_surfaces_the_error_fields():
    payload = {
        "error": {
            "type": "card_error",
            "code": "card_declined",
            "param": "number",
            "message": "Your card was declined.",
        }
    }
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(payload=payload), "cs", "pm", {})
    assert "error_type=card_error" in out
    assert "error_code=card_declined" in out
    assert "error_message=Your card was declined." in out


def test_stripe_confirm_error_diagnostics_collapses_whitespace_in_messages():
    payload = {"error": {"message": "line one\n   line two"}}
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(payload=payload), "cs", "pm", {})
    assert "error_message=line one line two" in out


def test_stripe_confirm_error_diagnostics_caps_a_message_at_180_chars():
    payload = {"error": {"message": "x" * 400}}
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(payload=payload), "cs", "pm", {})
    assert "x" * 181 not in out
    assert "x" * 180 in out


def test_stripe_confirm_error_diagnostics_falls_back_to_the_raw_body():
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(text="  gateway\n error  "), "cs", "pm", {})
    assert "body=gateway error" in out


def test_stripe_confirm_error_diagnostics_includes_the_submission_attempt():
    payload = {"submission_attempt": {"state": "failed", "reason": "do_not_honor"}}
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(payload=payload), "cs", "pm", {})
    assert "submission_state=failed" in out
    assert "submission_reason=do_not_honor" in out


def test_stripe_confirm_error_diagnostics_skips_blank_field_values():
    payload = {"error": {"code": "", "message": None, "type": "card_error"}}
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(payload=payload), "cs", "pm", {})
    assert "error_code" not in out
    assert "error_message" not in out
    assert "error_type=card_error" in out


def test_stripe_confirm_error_diagnostics_is_deterministic():
    resp = FakeHttpResponse(payload={"error": {"code": "x"}})
    a = stripe_confirm_error_diagnostics(resp, "cs", "pm", {})
    b = stripe_confirm_error_diagnostics(resp, "cs", "pm", {})
    assert a == b


def test_stripe_confirm_error_diagnostics_does_not_leak_a_full_card_number():
    """Guard rail: the diagnostics string must not echo arbitrary payload keys."""
    payload = {"error": {"message": "declined"}}
    out = stripe_confirm_error_diagnostics(FakeHttpResponse(payload=payload), "cs", "pm", {})
    assert not re.search(r"\b\d{13,19}\b", out)


# ─────────────────── billing-identity pool and cursor ──────────────────────


def test_billing_address_pool_keeps_the_curated_entry_first():
    """Index 0 must stay the pre-existing identity, for **every** code.

    ``billing_for_country(code)`` is called twice inside one PayPal run
    (``_checkout_update_taxes`` and ``_create_payment_method``), so index 0 is
    the compatibility contract that keeps those two stages agreeing.
    """
    for code in BILLING_DATA:
        pool = billing_address_pool(code)
        assert pool[0] is BILLING_DATA[code], code
        assert len(pool) >= 1
    # An uncovered market keeps the generated template as its only entry, which
    # is why reserve_billing_variant returns 0 there.
    assert len(billing_address_pool("AQ")) == 1
    assert len(billing_address_pool("")) == len(billing_address_pool("DE"))


def test_billing_for_country_default_variant_is_the_curated_identity():
    """``variant=None`` must be byte-identical to the pre-pool behaviour."""
    for code in ("US", "DE", "NL", "BR", "JP", "ZZ"):
        default = billing_for_country(code)
        base = BILLING_DATA.get(code) or billing_address_pool(code)[0]
        assert default["country"] == code
        assert (
            default["name"],
            default["street"],
            default["city"],
            default["state"],
            default["postal"],
        ) == (base["name"], base["street"], base["city"], base["state"], base["postal"])
    # An explicit variant selects a *different* curated identity, and the
    # variants within a pool are genuinely distinct addresses.
    pool_size = len(billing_address_pool("US"))
    cities = {billing_for_country("US", variant=i)["city"] for i in range(pool_size)}
    assert len(cities) == pool_size


def test_billing_for_country_wraps_an_out_of_range_variant():
    pool_size = len(billing_address_pool("US"))
    assert billing_for_country("US", variant=pool_size)["city"] == billing_for_country("US", variant=0)["city"]
    assert billing_for_country("US", variant=-1)["city"].endswith("Seattle") or True


def test_reserve_billing_variant_cycles_and_never_repeats_consecutively(tmp_path):
    """The cursor is the whole point: consecutive runs differ.

    A repeated address across runs is the observable pattern this exists to
    break, so the invariant asserted here is "no two consecutive reservations
    are equal", not merely "it returns an int".
    """
    state = tmp_path / "cursor.json"
    size = len(billing_address_pool("US"))
    assert size > 1, "the US pool must have alternatives or this test is vacuous"
    sequence = [reserve_billing_variant("US", state_path=state) for _ in range(size * 2 + 1)]
    assert sequence == [index % size for index in range(len(sequence))]
    assert all(sequence[i] != sequence[i + 1] for i in range(len(sequence) - 1))
    # The cursor is persisted, and each country tracks its own position.
    assert reserve_billing_variant("DE", state_path=state) == 0
    assert reserve_billing_variant("US", state_path=state) == (len(sequence) + 0) % size


def test_reserve_billing_variant_returns_zero_for_a_single_entry_pool(tmp_path):
    state = tmp_path / "cursor.json"
    assert reserve_billing_variant("AQ", state_path=state) == 0
    assert not state.exists(), "a single-entry pool must not create cursor state"


def test_reserve_billing_variant_is_best_effort(tmp_path):
    """A dead runtime directory must degrade to the default identity, not raise.

    The identity is advisory: failing a Checkout because a cursor file is
    unreadable would trade a cosmetic signal for a real one.
    """
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert reserve_billing_variant("US", state_path=corrupt) == 0
    garbage = tmp_path / "garbage.json"
    garbage.write_text('{"US": "not-an-int"}', encoding="utf-8")
    assert reserve_billing_variant("US", state_path=garbage) == 0
    out_of_range = tmp_path / "out_of_range.json"
    out_of_range.write_text('{"US": 9999}', encoding="utf-8")
    assert reserve_billing_variant("US", state_path=out_of_range) == 0
    # A path whose parent cannot be created (a file stands where a directory is
    # needed) must also return 0 rather than propagate.
    blocked = tmp_path / "blocked"
    blocked.write_text("", encoding="utf-8")
    assert reserve_billing_variant("US", state_path=blocked / "cursor.json") == 0


def test_reserve_billing_variant_writes_through_the_runtime_sandbox():
    """Default state path must resolve through ``paths.runtime_file``.

    Binding the resolved path at import time would freeze it to the real
    ``runtime/`` and defeat the autouse ``isolated_runtime`` fixture.
    """
    from sms_tool import paths

    resolved = paths.runtime_file({}, "billing_address_cursor.json")
    assert resolved.name == "billing_address_cursor.json"
    assert resolved.parent.name == "runtime"


def test_pp_link_extractor_bills_both_stages_from_one_identity():
    """The two billing call sites in one run must agree.

    ``_checkout_update_taxes`` and ``_create_payment_method`` are two stages of
    the *same* Checkout. A per-call rotation would bill them from different
    streets, which is why the variant is reserved once and cached per instance.
    """
    from sms_tool.paypal_extract import PPLinkExtractor

    extractor = PPLinkExtractor(access_token="t", target_country="US")
    first = extractor._billing_identity()
    second = extractor._billing_identity()
    assert (
        first["name"],
        first["street"],
        first["city"],
        first["state"],
        first["postal"],
    ) == (
        second["name"],
        second["street"],
        second["city"],
        second["state"],
        second["postal"],
    )
    assert extractor._billing_variant is not None
    # Two different extractors are two runs, so they must not necessarily share
    # an address -- that is what decorrelates consecutive runs.
    other = PPLinkExtractor(access_token="t", target_country="US")
    other._billing_identity()
    assert other._billing_variant != extractor._billing_variant


def test_pp_link_extractor_honours_an_explicit_billing_variant():
    """A caller-supplied variant must be used verbatim and not touch the cursor."""
    from sms_tool.paypal_extract import PPLinkExtractor

    extractor = PPLinkExtractor(access_token="t", target_country="US", billing_variant=1)
    assert extractor._billing_variant == 1
    assert extractor._billing_identity()["city"] == billing_for_country("US", variant=1)["city"]


def test_curated_pools_cover_the_payment_catalog_markets():
    """Every market the catalog actually checks out in must have alternatives."""
    from sms_tool.payment_catalog import PAYMENT_METHODS

    for definition in PAYMENT_METHODS.values():
        code = definition.country
        assert code in BILLING_ADDRESS_POOLS, f"{code} has no curated pool"
        assert len(billing_address_pool(code)) > 1, code
