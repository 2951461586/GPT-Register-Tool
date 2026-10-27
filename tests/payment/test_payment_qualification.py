"""支付资格: country-aware method selection, probe contract, batch summary.

The regression this pins (2026-10-26): the first version defaulted to GCash and
sent PH as ``billing_details.country`` for every account, so a non-PH exit got
``400 Billing country must match request country`` and the desktop showed the
liveness label ("测活失败 · HTTP 400") instead of a qualification verdict.

Every probe is injected and every country lookup is stubbed, so the tests assert
the *contract* the backend would have sent without touching the network."""

from __future__ import annotations

import pytest

from sms_tool.payment import qualification as pq
from sms_tool.payment import qualification_batch as pqb
from sms_tool.payment.qualification_states import (
    QUALIFICATION_AUTH_INVALID,
    QUALIFICATION_ELIGIBLE,
    QUALIFICATION_INELIGIBLE,
    QUALIFICATION_PROBE_FAILED,
    QUALIFICATION_UNKNOWN,
    qualification_label,
    qualification_state,
)

ELIGIBLE_RESULT = {
    "ok": True,
    "payment_method": "gcash",
    "eligible": True,
    "conclusive": True,
    "classification": "eligible",
    "decision": "payment_method_available",
    "status": "completed",
    "currency": "PHP",
    "amount": 0,
}
INELIGIBLE_NONZERO = {
    "ok": True,
    "payment_method": "gcash",
    "eligible": False,
    "conclusive": True,
    "classification": "ineligible",
    "decision": "nonzero_offer",
    "status": "completed",
}
# Verbatim shape of the observed failure: no decision, HTTP 400, and the
# server's sentence as the error text.
COUNTRY_400 = {
    "ok": False,
    "status": "failed",
    "error": "checkout returned HTTP 400: Billing country must match request country.",
    "error_code": "checkout_failed",
    "error_stage": "checkout_create",
    "eligible": None,
    "conclusive": False,
    "status_code": 400,
}


@pytest.fixture(autouse=True)
def _hermetic_proxy_and_country(monkeypatch):
    """No configured egress and no measurement unless a test asks for one."""
    monkeypatch.setattr(pq, "_select_proxy", lambda account, explicit, pool=None: (None, "", "test"))
    monkeypatch.setattr(pq, "_measured_country", lambda proxy: "")


# ── state vocabulary ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ELIGIBLE_RESULT, QUALIFICATION_ELIGIBLE),
        (INELIGIBLE_NONZERO, QUALIFICATION_INELIGIBLE),
        # A country mismatch is a deterministic "no", not a transport failure.
        (COUNTRY_400, QUALIFICATION_INELIGIBLE),
        ({"decision": "method_country_mismatch"}, QUALIFICATION_INELIGIBLE),
        ({"error": "checkout_unauthorized", "status_code": 401}, QUALIFICATION_AUTH_INVALID),
        ({"error": "missing_access_token"}, QUALIFICATION_AUTH_INVALID),
        ({"status": "failed", "error": "checkout_transport_failed"}, QUALIFICATION_PROBE_FAILED),
        ({"error": "curl: (28) timed out"}, QUALIFICATION_PROBE_FAILED),
        ({"eligible": None, "conclusive": False, "status": "unknown"}, QUALIFICATION_UNKNOWN),
        ({}, QUALIFICATION_UNKNOWN),
        (None, QUALIFICATION_UNKNOWN),
    ],
)
def test_qualification_state(result, expected):
    assert qualification_state(result) == expected


def test_country_mismatch_has_its_own_label():
    assert qualification_label(COUNTRY_400, "upi") == "无可用方式·地区不符"
    assert qualification_label(INELIGIBLE_NONZERO) == "无0元优惠"
    assert qualification_label(ELIGIBLE_RESULT, "gcash") == "可用·GCash"
    assert qualification_label({"error": "token_invalid"}) == "AT失效"
    assert qualification_label({"status": "failed", "error": "transport down"}) == "检测失败"
    assert qualification_label({"eligible": None, "conclusive": False}) == "未确定"


# ── request identity (2026-10-27 risk-block fix) ────────────────────────────
def test_auth_context_reuses_the_account_cookie_header_and_its_own_did():
    """A Checkout is a same-site action: it needs the session cookies, not just a did.

    Measured 2026-10-27: sending only ``oai-did=<uuid>`` while the AT belonged to
    a real browser session produced ``400 Our systems have detected unusual
    activity``.
    """
    context = pq._auth_context({"cookie_header": "a=1; oai-did=cookie-did", "device_id": "column-did"})
    assert context["device_id"] == "cookie-did"
    assert context["cookie_header"] == "a=1; oai-did=cookie-did"


def test_auth_context_falls_back_to_identity_then_to_the_session_token():
    from_identity = pq._auth_context({"identity_context": {"device_id": "ctx-did"}})
    assert from_identity["device_id"] == "ctx-did"
    assert from_identity["cookie_header"] == "oai-did=ctx-did"

    from_session = pq._auth_context({"auth_session": {"sessionToken": "SESSION"}})
    assert "__Secure-next-auth.session-token=SESSION" in from_session["cookie_header"]
    assert f"oai-did={from_session['device_id']}" in from_session["cookie_header"]


def test_a_risk_block_has_its_own_label_but_stays_probe_failed():
    risk = {
        "status": "failed",
        "error": "checkout HTTP 400 code=Our systems have detected unusual activity.",
        "error_code": "checkout_risk_blocked",
    }
    assert qualification_state(risk) == QUALIFICATION_PROBE_FAILED
    assert qualification_label(risk, "gcash") == "风控拦截"


def test_a_risk_block_is_retried_on_another_exit():
    # Exit/device-dependent, unlike a plain HTTP 400 from a contract check.
    assert (
        pqb._retryable_transport(
            {"error": "checkout HTTP 400 code=Our systems have detected unusual activity.", "status_code": 400}
        )
        is True
    )


# ── country → methods ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("country", "expected"),
    [
        # Local wallets first, then the country-agnostic pair (PayPal / card),
        # deduplicated. JP has no local wallet in the catalogue and must still
        # get a real probe instead of a blanket "无可用方式".
        ("PH", ["gcash", "grabpay", "direct_card", "paypal"]),
        ("VN", ["momo", "paypal", "direct_card"]),
        ("IN", ["upi", "paypal", "direct_card"]),
        ("ID", ["gopay", "qris", "paypal", "direct_card"]),
        ("KR", ["kakao", "naver_pay", "paypal", "direct_card"]),
        ("JP", ["paypal", "direct_card"]),
        ("US", ["paypal", "direct_card"]),
        # Empty means "no country, do not probe"; a well-formed but unknown code
        # still probes the agnostic methods and lets the server decide.
        ("", []),
        ("ZZ", ["paypal", "direct_card"]),
    ],
)
def test_methods_for_country_comes_from_the_catalogue(country, expected):
    assert pq.methods_for_country(country) == expected


def test_parse_method_selection_accepts_lists_and_ignores_auto():
    assert pq.parse_method_selection("auto") == []
    assert pq.parse_method_selection("") == []
    assert pq.parse_method_selection("gcash, momo") == ["gcash", "momo"]
    assert pq.parse_method_selection(["upi", "UPI "]) == ["upi"]
    assert pq.parse_method_selection("GCash-PH") == ["gcash_ph"]


# ── country resolution ───────────────────────────────────────────────────────
def test_explicit_country_wins():
    assert pq.resolve_qualification_country({"registration_country": "VN"}, None, "ph") == ("PH", "explicit")


def test_measured_exit_wins_over_the_stored_country(monkeypatch):
    """The server compares against the *request* country, which follows the exit."""
    monkeypatch.setattr(pq, "_measured_country", lambda proxy: "VN")
    country, source = pq.resolve_qualification_country({"registration_country": "PH"}, "http://p:1")
    assert (country, source) == ("VN", "measured")


def test_registration_country_is_the_first_fallback():
    assert pq.resolve_qualification_country({"registration_country": "in"}, None) == ("IN", "registration_country")


def test_identity_context_is_the_last_resort():
    account = {"identity_context": {"country": "id"}}
    assert pq.resolve_qualification_country(account, None) == ("ID", "identity_context")


def test_unknown_country_stays_empty():
    assert pq.resolve_qualification_country({}, None) == ("", "unknown")


# ── probe contract ───────────────────────────────────────────────────────────
def _probe_map(mapping):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        value = mapping.get(kwargs["payment_method"], INELIGIBLE_NONZERO)
        return dict(value, payment_method=kwargs["payment_method"])

    return fake, calls


def test_ph_account_auto_selects_ph_methods_and_sends_the_ph_contract():
    fake, calls = _probe_map({"gcash": ELIGIBLE_RESULT})
    result = pq.check_payment_qualification(
        {"email": "a@example.com", "access_token": "AT", "registration_country": "PH"},
        probe=fake,
        probe_all_methods=True,
    )
    assert [call["payment_method"] for call in calls] == ["gcash", "grabpay", "direct_card", "paypal"]
    first = calls[0]
    assert first["billing_country"] == "PH"
    assert first["checkout_country"] == "PH"
    assert first["currency"] == "PHP"
    assert first["payment_locale"] == "en-PH"
    assert first["browser_locale"] == "en-PH"
    assert first["browser_timezone"] == "Asia/Manila"
    assert result["payment_qualification_state"] == QUALIFICATION_ELIGIBLE
    assert result["payment_qualification"] == "可用·GCash"
    assert result["eligible_methods"] == ["gcash"]
    assert result["country"] == "PH"
    assert len(result["payment_methods"]) == 4


def test_vn_account_never_sends_a_gcash_request():
    fake, calls = _probe_map({})
    result = pq.check_payment_qualification(
        {"email": "a@example.com", "access_token": "AT", "registration_country": "VN"},
        probe=fake,
    )
    probed = [call["payment_method"] for call in calls]
    assert "gcash" not in probed
    assert probed == ["momo", "paypal", "direct_card"]
    assert calls[0]["billing_country"] == "VN"
    assert calls[0]["currency"] == "VND"
    assert result["payment_qualification_state"] == QUALIFICATION_INELIGIBLE


def test_a_jp_account_still_gets_the_agnostic_methods_with_a_jp_contract():
    fake, calls = _probe_map({"paypal": ELIGIBLE_RESULT})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "JP"},
        probe=fake,
        probe_all_methods=True,
    )
    assert [call["payment_method"] for call in calls] == ["paypal", "direct_card"]
    assert calls[0]["billing_country"] == "JP"
    assert calls[0]["browser_locale"] == "ja-JP"
    assert result["payment_qualification_state"] == QUALIFICATION_ELIGIBLE
    assert result["payment_qualification"] == "可用·PayPal"


def test_first_eligible_only_short_circuits():
    fake, calls = _probe_map({"gcash": ELIGIBLE_RESULT})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        probe=fake,
        last_eligible_only=True,
    )
    assert len(calls) == 1
    assert result["eligible_methods"] == ["gcash"]


def test_explicit_method_outside_the_exit_country_is_skipped_without_a_probe():
    fake, calls = _probe_map({"gcash": ELIGIBLE_RESULT})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "VN"},
        method="gcash",
        probe=fake,
    )
    assert calls == []
    assert result["payment_qualification_state"] == QUALIFICATION_INELIGIBLE
    assert result["payment_qualification"] == "无可用方式·地区不符"
    assert result["payment_methods"][0]["error_code"] == "method_country_mismatch"


def test_all_country_400s_report_the_region_mismatch_not_a_probe_failure():
    fake, _ = _probe_map({})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "IN"},
        method="upi",
        probe=lambda **kwargs: dict(COUNTRY_400),
    )
    assert result["payment_qualification_state"] == QUALIFICATION_INELIGIBLE
    assert result["payment_qualification"] == "无可用方式·地区不符"
    assert "Billing country must match" in result["error"]


def test_unknown_country_returns_unknown_without_a_probe():
    fake, calls = _probe_map({})
    result = pq.check_payment_qualification({"access_token": "AT"}, probe=fake)
    assert calls == []
    assert result["payment_qualification_state"] == QUALIFICATION_UNKNOWN
    assert result["error"] == "country_unknown"


def test_missing_access_token_short_circuits():
    fake, calls = _probe_map({})
    result = pq.check_payment_qualification({"email": "a@example.com", "registration_country": "PH"}, probe=fake)
    assert calls == []
    assert result["payment_qualification_state"] == QUALIFICATION_AUTH_INVALID


def test_a_401_on_one_method_aggregates_to_auth_invalid():
    def fake(**kwargs):
        return {"status": "failed", "error": "checkout_unauthorized", "status_code": 401}

    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"}, method="gcash", probe=fake
    )
    assert result["payment_qualification_state"] == QUALIFICATION_AUTH_INVALID


def test_a_raising_probe_becomes_probe_failed_and_redacts_the_proxy():
    def boom(**kwargs):
        raise RuntimeError("connect to http://user:secret@ph.example:8080 failed")

    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="gcash",
        proxy="http://user:secret@ph.example:8080",
        probe=boom,
    )
    assert result["payment_qualification_state"] == QUALIFICATION_PROBE_FAILED
    assert "secret" not in result["error"]


def test_non_dict_probe_answer_is_probe_failed():
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"}, method="gcash", probe=lambda **kwargs: "nope"
    )
    assert result["payment_qualification_state"] == QUALIFICATION_PROBE_FAILED
    assert result["payment_methods"][0]["error_code"] == "qualification_probe_invalid_result"


def test_configured_method_defaults_to_auto_and_reads_a_list():
    assert pq.configured_qualification_method({}) == "auto"
    assert pq.configured_qualification_method({"payment_qualification": {"method": "paypal"}}) == "paypal"
    assert pq.configured_qualification_method({"payment_qualification": {"method": ["gcash", "momo"]}}) == (
        "gcash,momo"
    )
    assert pq.configured_qualification_country({"payment_qualification": {"country": "ph"}}) == "PH"


# ── batch bookkeeping ───────────────────────────────────────────────────────
def test_row_marks_only_conclusive_states_as_ok_and_carries_the_display_badge():
    row = pqb._row(
        "a@x",
        {
            "payment_qualification_state": QUALIFICATION_ELIGIBLE,
            "payment_qualification": "可用·GCash",
            "payment_methods": [{"method": "gcash"}],
            "country": "PH",
        },
        persisted=True,
    )
    assert row["ok"] is True
    assert row["payment_qualification_display"] == "可用·GCash"
    assert row["payment_methods"] == [{"method": "gcash"}]
    assert row["country"] == "PH"
    assert pqb._row("a@x", {"payment_qualification_state": QUALIFICATION_INELIGIBLE})["ok"] is True
    assert pqb._row("a@x", {"payment_qualification_state": QUALIFICATION_PROBE_FAILED})["ok"] is False


def test_summary_counts_every_state_and_separates_eligibility():
    rows = [
        pqb._row("a@x", {"payment_qualification_state": QUALIFICATION_ELIGIBLE}, persisted=True),
        pqb._row("b@x", {"payment_qualification_state": QUALIFICATION_INELIGIBLE}, persisted=True),
        pqb._row("c@x", {"payment_qualification_state": QUALIFICATION_AUTH_INVALID}, persisted=True),
        pqb._row("d@x", {"payment_qualification_state": QUALIFICATION_PROBE_FAILED}),
    ]
    summary = pqb._summary(rows, method="auto")
    assert summary["total"] == 4
    assert summary["success"] == 2
    assert summary["eligible"] == 1
    assert summary["ineligible"] == 1
    assert summary["auth_invalid"] == 1
    assert summary["probe_failed"] == 1
    assert summary["persist_failed"] == 1
    assert summary["ok"] is False


def test_transport_retry_requires_no_http_answer():
    assert pqb._retryable_transport({"error": "curl: (28) timed out"}) is True
    # An HTTP status proves the exit worked, so it must not rotate.
    assert pqb._retryable_transport({"error": "timeout", "status_code": 400}) is False
    assert pqb._retryable_transport({"ok": True}) is False


def test_env_failure_class_uses_the_shared_breaker_vocabulary():
    # ``BatchCircuitBreaker`` only trips on network/auth_state; the old
    # transport/unauthorized values matched nothing and made the breaker inert.
    assert (
        pqb._env_failure_class(
            {"payment_qualification_state": QUALIFICATION_PROBE_FAILED, "error": "curl: (28) timed out"}
        )
        == "network"
    )
    assert (
        pqb._env_failure_class(
            {"payment_qualification_state": QUALIFICATION_PROBE_FAILED, "error": "400 unusual activity"}
        )
        == "network"
    )
    assert pqb._env_failure_class({"payment_qualification_state": QUALIFICATION_PROBE_FAILED}) == "probe"
    assert pqb._env_failure_class({"payment_qualification_state": QUALIFICATION_AUTH_INVALID}) == "account"
    assert pqb._env_failure_class({"payment_qualification_state": QUALIFICATION_UNKNOWN}) == "probe"


def test_default_stops_after_the_first_eligible_method():
    fake, calls = _probe_map({"gcash": ELIGIBLE_RESULT})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        probe=fake,
    )
    assert [call["payment_method"] for call in calls] == ["gcash"]
    assert result["payment_qualification_state"] == QUALIFICATION_ELIGIBLE


def test_unknown_method_is_a_row_not_a_crash():
    fake, calls = _probe_map({"gcash": ELIGIBLE_RESULT})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="not_a_method",
        probe=fake,
    )
    assert calls == []
    assert result["payment_qualification_state"] == QUALIFICATION_UNKNOWN
    assert result["payment_methods"][0]["error_code"] == "unknown_method"


def test_country_agnostic_methods_use_the_exit_currency():
    assert pq._probe_kwargs("direct_card", "JP")["currency"] == "JPY"
    assert pq._probe_kwargs("direct_card", "GB")["currency"] == "GBP"
    assert pq._probe_kwargs("paypal", "GB")["currency"] == "GBP"


def test_a_hint_only_geo_answer_is_not_reported_as_measured(monkeypatch):
    from sms_tool.geo import ProxyGeo

    # The autouse fixture stubs ``_measured_country`` itself; undo that patch so
    # this test exercises the real function against a fake resolver.
    monkeypatch.undo()
    monkeypatch.setattr(
        "sms_tool.geo.resolve_proxy_geo",
        lambda proxy, **kwargs: ProxyGeo(country="VN", source="hint"),
    )
    assert pq._measured_country("http://user-region-VN-sid-a:pass@h:1") == ""

    monkeypatch.setattr(
        "sms_tool.geo.resolve_proxy_geo",
        lambda proxy, **kwargs: ProxyGeo(country="VN", timezone="Asia/Ho_Chi_Minh", source="probe"),
    )
    assert pq._measured_country("http://user-region-VN-sid-a:pass@h:1") == "VN"


def test_a_socks5_proxy_in_a_probe_error_is_redacted(monkeypatch):
    def raising(**kwargs):
        raise RuntimeError("socks5://alice:secret@host:1080 failed")

    row = pq._run_method(raising, "AT", {}, "paypal", "US", "socks5://alice:secret@host:1080", 5)
    assert "secret" not in row["error"]


# ── dedicated egress / stripe preflight / badge provenance ──────────────────
def test_dedicated_qualification_pool_reads_config_and_lane():
    assert pq.configured_qualification_proxy_pool({"payment_qualification": {"proxy_pool": ["http://u:p@h:1"]}}) == [
        "http://u:p@h:1"
    ]
    assert pq.configured_qualification_proxy_pool(
        {"proxy": {"lanes": {"payment_qualification": ["http://u:p@h:2"]}}}
    ) == ["http://u:p@h:2"]


def test_max_proxy_attempts_is_clamped():
    assert pq.configured_max_proxy_attempts({"payment_qualification": {"max_proxy_attempts": 0}}) == 1
    assert pq.configured_max_proxy_attempts({"payment_qualification": {"max_proxy_attempts": 99}}) == 32
    assert pq.configured_max_proxy_attempts({"payment_qualification": {"max_proxy_attempts": "bad"}}) == 3


def test_checkout_kind_reaches_the_badge_and_the_row():
    result = dict(ELIGIBLE_RESULT, checkout_kind="oaics")
    assert qualification_label(result, "paypal") == "可用·PayPal（自定义）"
    row = pq._method_row("paypal", result)
    assert row["checkout_kind"] == "oaics"
    assert pq._aggregate_label(QUALIFICATION_ELIGIBLE, [row]) == "可用·PayPal（自定义）"


def test_a_transient_failure_keeps_the_last_conclusive_badge():
    from sms_tool import desktop_read
    from sms_tool.store.markers import _update_last_conclusive

    qualification = {"status": "可用·PayPal", "state": "eligible", "updated_at": 1}
    _update_last_conclusive(qualification, "检测失败", "probe_failed", 2)
    qualification["status"] = "检测失败"
    qualification["state"] = "probe_failed"
    assert desktop_read._payment_qualification_presentation({"payment_qualification": qualification}) == (
        "检测失败（上次：可用·PayPal）",
        "probe_failed",
    )


def test_stripe_reachability_rejects_proxy_acl_denials():
    from sms_tool.payment import qualification_batch as pqb

    assert pqb._stripe_reachable("http://p:1", probe=lambda proxy, timeout: (404, "")) is True
    assert pqb._stripe_reachable("http://p:1", probe=lambda proxy, timeout: (200, "")) is True
    assert (
        pqb._stripe_reachable(
            "http://p:1", probe=lambda proxy, timeout: (503, "Tunnel connection failed: 503 Target host denied")
        )
        is False
    )
    assert pqb._stripe_reachable("http://p:1", probe=lambda proxy, timeout: (0, "timeout")) is False


def test_stripe_preflight_needs_one_reachable_candidate(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "warn")
    calls: list[str] = []

    def probe(proxy, timeout):
        calls.append(proxy)
        return (404, "") if proxy.endswith("a:1") else (503, "Target host denied")

    result = pqb._stripe_preflight(["http://a:1", "http://b:2"], probe=probe, source="dedicated")
    assert result["ok"] is True
    # Head first: one reachable exit ends the preflight after ONE request. Nine
    # exits used to be dialled even when the first already answered.
    assert calls == ["http://a:1"]
    assert result["checked"] == 1
    assert result["reachable"] == 1
    assert result["skipped"] is False
    assert result["source"] == "dedicated"
    assert result["first_failure"] is None
    assert pqb._stripe_preflight([], probe=lambda proxy, timeout: (404, ""))["ok"] is True


def test_stripe_preflight_widens_to_the_rest_only_when_the_head_is_denied(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "warn")
    calls: list[str] = []

    def probe(proxy, timeout):
        calls.append(proxy)
        return (503, "Target host denied") if proxy.endswith("a:1") else (404, "")

    result = pqb._stripe_preflight(["http://a:1", "http://b:2", "http://c:3"], probe=probe)
    assert result["ok"] is True
    assert calls[0] == "http://a:1"
    assert sorted(calls) == ["http://a:1", "http://b:2", "http://c:3"]
    assert result["checked"] == 3
    assert result["reachable"] == 2


def test_stripe_preflight_quotes_the_first_denial_once(monkeypatch):
    """The evidence must come from the same probe, with no second request."""
    from sms_tool.payment import qualification_batch as pqb

    calls: list[str] = []

    def probe(proxy, timeout):
        calls.append(proxy)
        return (503, "Tunnel connection failed: 503 Target host denied")

    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "warn")
    result = pqb._stripe_preflight(["http://u:p@denied.example:8080"], probe=probe, source="dedicated")

    assert calls == ["http://u:p@denied.example:8080"]
    assert result["ok"] is False
    assert result["reason"] == "unreachable"
    failure = result["first_failure"]
    assert failure["host"] == "denied.example:8080"
    assert failure["status"] == 503
    assert "Target host denied" in failure["reason"]
    # Credentials never reach the operator-facing detail.
    assert "u:p" not in failure["reason"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "warn"),
        ("warn", "warn"),
        ("", "warn"),
        ("typo", "warn"),
        ("strict", "strict"),
        ("abort", "strict"),
        (False, "off"),
        ("off", "off"),
        ("0", "off"),
    ],
)
def test_stripe_preflight_mode_parsing(monkeypatch, value, expected):
    """An unrecognised value must fall back to ``warn`` so a typo cannot block a batch."""
    from sms_tool.payment import qualification_batch as pqb

    monkeypatch.setattr(
        pqb,
        "_runtime_config",
        lambda: {"payment_qualification": {"stripe_preflight": value}},
    )
    assert pqb._stripe_preflight_mode() == expected


def test_paypal_stripe_init_failure_keeps_its_stage(monkeypatch):
    from sms_tool.payment import capability

    class FakeExtractor:
        enable_promotion = True
        checkout_country = "US"
        checkout_currency = "USD"

        def __init__(self, **kwargs):
            pass

        def _create_checkout(self):
            return {"cs_id": "cs_test", "processor_entity": "openai_ie"}

        def checkout_update_promotion(self, *args, **kwargs):
            return True

        def _stripe_init(self, *args, **kwargs):
            raise RuntimeError("Tunnel connection failed: 503 Target host denied")

    monkeypatch.setattr("sms_tool.paypal.extract.PPLinkExtractor", FakeExtractor)
    result = capability._paypal_capability_probe(
        access_token="AT",
        auth_context={},
        checkout_proxy="",
        provider_proxy="",
        stripe_init_proxy="",
        payment_method_proxy="",
        confirm_proxy="",
        approve_proxy="",
        promotion_proxy="",
        target_country="US",
        checkout_country="US",
        promo_campaign_id="",
        require_zero=True,
        stage_proxy_countries=None,
        timeout=5,
    )
    assert result["error_code"] == "stripe_init_transport_failed"
    assert result["error_stage"] == "stripe_init"
    assert result["retryable"] is True


def test_summary_carries_the_abort_reason():
    summary = pqb._summary([], method="auto", aborted=True, abort_reason="stripe_unreachable")
    assert summary["aborted"] is True
    assert summary["abort_reason"] == "stripe_unreachable"


def test_dedicated_qualification_pool_outranks_the_desktop_explicit_proxy(monkeypatch):
    """Regression (measured 2026-10-27): the dedicated exit was tried LAST.

    The desktop always passes the registration exit as ``--proxy``, and
    ``operation_proxy_candidates`` puts explicit input first. Passing the
    dedicated pool as ``pool`` demoted it behind the ChatGPT-only signup exit,
    so ``payment_qualification.proxy_pool`` never took effect.
    """
    from sms_tool.payment import qualification_batch as pqb

    monkeypatch.setattr(pqb, "configured_qualification_proxy_pool", lambda: ["http://dedicated:1"])
    explicit, pool = pqb._routing_inputs("http://desktop:9", ["http://desktop:9", "http://desktop:8"])
    assert explicit == ["http://dedicated:1"]
    assert pool == ["http://desktop:9", "http://desktop:8"]

    monkeypatch.setattr(pqb, "configured_qualification_proxy_pool", lambda: [])
    assert pqb._routing_inputs("http://desktop:9", ["http://desktop:8"]) == (
        "http://desktop:9",
        ["http://desktop:8"],
    )


def test_egress_candidates_put_the_dedicated_pool_first(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    monkeypatch.setattr(pqb, "configured_qualification_proxy_pool", lambda: ["http://dedicated:1"])
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    candidates = pqb._egress_candidates("http://desktop:9", ["http://desktop:9", "http://desktop:8"])

    assert candidates[0] == "http://dedicated:1"
    # The desktop route stays available as a fallback, deduplicated.
    assert candidates.count("http://desktop:9") == 1
    assert "http://desktop:8" in candidates


def test_probe_rotation_tries_the_dedicated_pool_before_the_registration_exit(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    tried: list[str] = []

    def fake_probe(account, *, proxy, timeout, method, country, probe_all_methods=None, pinned_proxy=None):
        tried.append(proxy)
        # A transport failure is what makes the rotation worth another egress.
        return {"ok": False, "status_code": 0, "error": "connection reset by peer"}

    monkeypatch.setattr(pqb, "configured_qualification_proxy_pool", lambda: ["http://dedicated:1"])
    monkeypatch.setattr(pqb, "configured_max_proxy_attempts", lambda: 5)
    monkeypatch.setattr(pqb, "check_payment_qualification", fake_probe)
    monkeypatch.setattr(pqb, "_runtime_config", lambda: {})
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: None)

    pqb._probe_with_rotation(
        "run",
        {"email": "a@example.com"},
        "http://desktop:9",
        ["http://desktop:9"],
        5,
        "auto",
        "US",
        1,
    )

    assert tried[:2] == ["http://dedicated:1", "http://desktop:9"]


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        ({"payment_qualification": {"last_result": {"payment_methods": [{"checkout_kind": "oaics"}]}}}, True),
        ({"payment_qualification": {"last_result": {"checkout_kind": "oaics"}}}, True),
        (
            {
                "payment_qualification": {
                    "last_result": {"payment_methods": [{"checkout_kind": "oaics"}, {"checkout_kind": "stripe"}]}
                }
            },
            False,
        ),
        ({"payment_qualification": {"last_result": {"payment_methods": [{"checkout_kind": ""}]}}}, False),
        ({"payment_qualification": {"last_result": {}}}, False),
        ({}, False),
    ],
)
def test_last_checkouts_are_custom(account, expected):
    from sms_tool.payment import qualification_batch as pqb

    assert pqb._last_checkouts_are_custom([account, account]) is expected


def test_stripe_preflight_is_skipped_when_every_last_checkout_was_custom(monkeypatch):
    """oaics checkouts never call api.stripe.com, so gating on Stripe is pointless."""
    from sms_tool.payment import qualification_batch as pqb

    def boom(*args, **kwargs):
        raise AssertionError("Stripe preflight must not run for an oaics-only selection")

    monkeypatch.setattr(
        pqb,
        "_load_accounts",
        lambda *args, **kwargs: [
            {
                "email": "a@example.com",
                "access_token": "AT",
                "payment_qualification": {"last_result": {"checkout_kind": "oaics"}},
            }
        ],
    )
    monkeypatch.setattr(pqb, "_egress_candidates", lambda *args, **kwargs: ["http://p:1"])
    monkeypatch.setattr(pqb, "_preflight", lambda candidates: {"ok": True, "checked": 1})
    monkeypatch.setattr(pqb, "_stripe_preflight", boom)
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(pqb, "_persist", lambda email, probe: True)
    monkeypatch.setattr(
        pqb,
        "_probe_with_rotation",
        lambda *args, **kwargs: dict(
            ELIGIBLE_RESULT,
            checkout_kind="oaics",
            payment_qualification_state=QUALIFICATION_ELIGIBLE,
            payment_qualification=qualification_label(dict(ELIGIBLE_RESULT, checkout_kind="oaics"), "paypal"),
        ),
    )

    result = pqb.refresh_payment_qualification(emails=["a@example.com"])

    assert result["aborted"] is False
    assert result["eligible"] == 1
    assert result["stripe_preflight"]["skipped"] is True
    assert result["stripe_preflight"]["reason"] == "oaics_only"


def test_stripe_preflight_warns_by_default_and_keeps_probing(monkeypatch):
    """A Stripe-denied pool can still answer via oaics: warn, never abort."""
    from sms_tool.payment import qualification_batch as pqb

    events: list[dict] = []
    monkeypatch.setattr(pqb, "_load_accounts", lambda *args, **kwargs: [{"email": "a@example.com"}])
    monkeypatch.setattr(pqb, "_egress_candidates", lambda *args, **kwargs: ["http://p:1"])
    monkeypatch.setattr(pqb, "_preflight", lambda candidates: {"ok": True, "checked": 1})
    monkeypatch.setattr(
        pqb,
        "_stripe_preflight",
        lambda candidates, **kwargs: {
            "ok": False,
            "checked": 1,
            "reachable": 0,
            "skipped": False,
            "reason": "unreachable",
            "source": "dedicated",
            "first_failure": {"host": "denied.example:8080", "status": 503, "reason": "Target host denied"},
        },
    )
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: events.append({"stage": args[1], "status": args[2]}))
    monkeypatch.setattr(pqb, "_persist", lambda email, probe: True)
    monkeypatch.setattr(
        pqb,
        "_probe_with_rotation",
        lambda *args, **kwargs: dict(
            ELIGIBLE_RESULT, payment_qualification_state=QUALIFICATION_ELIGIBLE, payment_qualification="可用"
        ),
    )

    result = pqb.refresh_payment_qualification(emails=["a@example.com"])

    assert result["aborted"] is False
    assert result["eligible"] == 1
    assert {"stage": "batch_warning", "status": "warning"} in events
    assert result["warnings"][0]["code"] == "stripe_unreachable"
    assert result["stripe_preflight"]["ok"] is False


def test_stripe_preflight_strict_still_aborts_before_any_account(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    events: list[dict] = []
    monkeypatch.setattr(pqb, "_load_accounts", lambda *args, **kwargs: [{"email": "a@example.com"}])
    monkeypatch.setattr(pqb, "_egress_candidates", lambda *args, **kwargs: ["http://p:1"])
    monkeypatch.setattr(pqb, "_preflight", lambda candidates: {"ok": True, "checked": 1})
    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "strict")
    monkeypatch.setattr(
        pqb,
        "_stripe_preflight",
        lambda candidates, **kwargs: {
            "ok": False,
            "checked": 1,
            "reachable": 0,
            "skipped": False,
            "reason": "unreachable",
            "source": "dedicated",
            "first_failure": None,
        },
    )
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: events.append({"stage": args[1]}))

    result = pqb.refresh_payment_qualification(emails=["a@example.com"])

    assert result["aborted"] is True
    assert result["abort_reason"] == "stripe_unreachable"
    assert any(event["stage"] == "batch_aborted" for event in events)


def test_stripe_detail_reports_evidence_and_never_reasks_for_a_configured_field():
    from sms_tool.payment import qualification_batch as pqb

    dedicated = pqb._stripe_detail(
        {
            "checked": 9,
            "source": "dedicated",
            "first_failure": {"host": "eu.gw.proxylane.dev:10000", "status": 503, "reason": "Target host denied"},
        },
        strict=False,
    )
    assert "已配置的 9 条支付资格专用出口" in dedicated
    assert "eu.gw.proxylane.dev:10000" in dedicated
    assert "503" in dedicated
    assert "Target host denied" in dedicated
    # The old message told the operator to use the field they had just filled.
    assert "请改用" not in dedicated
    assert "继续检测" in dedicated

    fallback = pqb._stripe_detail(
        {"checked": 9, "source": "fallback", "first_failure": None},
        strict=True,
    )
    assert "未配置支付资格专用出口" in fallback
    assert "已中止" in fallback


def test_single_account_probe_passes_the_dedicated_pool_as_explicit_input(monkeypatch):
    """Same precedence as the batch: dedicated first, desktop route as fallback."""
    captured = {}

    def fake_select(account, explicit, pool=None):
        captured["explicit"] = explicit
        captured["pool"] = pool
        return None, "", "test"

    monkeypatch.setattr(pq, "_select_proxy", fake_select)
    monkeypatch.setattr(pq, "configured_qualification_proxy_pool", lambda: ["http://dedicated:1"])
    pq.check_payment_qualification(
        {"access_token": "AT"},
        proxy="http://desktop:9",
        country="US",
        probe=lambda **kwargs: dict(ELIGIBLE_RESULT, checkout_kind="oaics"),
    )
    assert captured["explicit"] == ["http://dedicated:1"]
    assert captured["pool"] == "http://desktop:9"


def test_batch_aborts_before_accounts_when_stripe_is_unreachable(monkeypatch):
    from sms_tool.payment import qualification_batch as pqb

    events: list[dict] = []
    monkeypatch.setattr(pqb, "_load_accounts", lambda *args, **kwargs: [{"email": "a@example.com"}])
    monkeypatch.setattr(pqb, "_egress_candidates", lambda *args, **kwargs: ["http://p:1"])
    monkeypatch.setattr(pqb, "_preflight", lambda candidates: {"ok": True, "checked": 1})
    monkeypatch.setattr(pqb, "_stripe_preflight_mode", lambda: "strict")
    monkeypatch.setattr(
        pqb,
        "_stripe_preflight",
        lambda candidates, **kwargs: {
            "ok": False,
            "checked": 1,
            "reachable": 0,
            "skipped": False,
            "reason": "unreachable",
            "source": "fallback",
            "first_failure": None,
        },
    )
    monkeypatch.setattr(pqb, "_emit", lambda *args, **kwargs: events.append({"args": args, "kwargs": kwargs}))

    result = pqb.refresh_payment_qualification(emails=["a@example.com"])
    assert result["aborted"] is True
    assert result["abort_reason"] == "stripe_unreachable"
    assert any(event["args"][1] == "batch_aborted" for event in events)


# ── shared Checkout: one create answers every method with the same contract ──
SHARED_EVIDENCE = dict(
    ELIGIBLE_RESULT,
    payment_method="grabpay",
    payment_method_types=["card", "grabpay"],
    status_code=200,
    http_status=200,
)


def test_a_second_method_with_the_same_contract_reuses_the_first_checkout():
    """Measured 2026-10-27: ``grabpay`` and ``direct_card`` send the same create.

    ``checkout_payload()`` carries no payment method, so a per-method loop bought
    the same answer twice. Only the *availability* differs, and the evidence
    already lists the published methods.
    """
    fake, calls = _probe_map({"grabpay": SHARED_EVIDENCE})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="grabpay,direct_card",
        probe=fake,
    )

    assert [call["payment_method"] for call in calls] == ["grabpay"]
    rows = {row["method"]: row for row in result["payment_methods"]}
    assert rows["grabpay"]["evidence_source"] == "checkout"
    assert rows["direct_card"]["evidence_source"] == "shared_checkout"
    assert rows["direct_card"]["derived_from"] == "grabpay"
    # ``card`` is published by that checkout, so the derived verdict is eligible.
    assert rows["direct_card"]["state"] == QUALIFICATION_ELIGIBLE
    assert result["payment_qualification_state"] == QUALIFICATION_ELIGIBLE


def test_a_derived_method_that_is_not_published_is_ineligible():
    fake, calls = _probe_map({"grabpay": dict(SHARED_EVIDENCE, payment_method_types=["grabpay"])})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="grabpay,direct_card",
        probe=fake,
    )

    assert len(calls) == 1
    row = next(item for item in result["payment_methods"] if item["method"] == "direct_card")
    assert row["state"] == QUALIFICATION_INELIGIBLE
    assert row["decision"] == "payment_method_unavailable"
    assert row["conclusive"] is True
    assert row["evidence_source"] == "shared_checkout"


def test_evidence_without_a_method_list_is_not_used_to_derive():
    """No inventory (a failure, or an unnamed ``oaics`` checkout) ⇒ probe for real."""
    fake, calls = _probe_map({})
    pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="grabpay,direct_card",
        probe=fake,
    )
    assert [call["payment_method"] for call in calls] == ["grabpay", "direct_card"]


def test_the_diagnostic_grid_probes_every_method_even_with_shared_evidence():
    fake, calls = _probe_map({"grabpay": SHARED_EVIDENCE})
    pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="grabpay,direct_card",
        probe=fake,
        probe_all_methods=True,
    )
    assert [call["payment_method"] for call in calls] == ["grabpay", "direct_card"]


def test_paypal_and_gcash_never_share_each_others_checkout():
    """They own separate transports, so the contract key must keep them apart."""
    fake, calls = _probe_map({"gcash": dict(ELIGIBLE_RESULT, payment_method_types=["card"])})
    pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="gcash,paypal,direct_card",
        probe=fake,
        probe_all_methods=True,
    )
    # gcash (own probe) → paypal (own probe) → direct_card (stripe path) are three
    # different keys, so the gcash inventory may not answer the other two.
    assert [call["payment_method"] for call in calls] == ["gcash", "paypal", "direct_card"]


def test_an_unmapped_exit_currency_is_reported_instead_of_billed_in_php():
    fake, calls = _probe_map({})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "MX"},
        method="direct_card",
        probe=fake,
    )
    assert calls == []
    row = result["payment_methods"][0]
    assert row["error_code"] == "currency_unknown"
    assert row["state"] == QUALIFICATION_UNKNOWN
    assert result["payment_qualification_state"] == QUALIFICATION_UNKNOWN


def test_http_status_and_retryable_reach_the_row_and_the_account_result():
    def fake(**kwargs):
        return {
            "status": "failed",
            "error": "checkout returned HTTP 403",
            "error_code": "checkout_failed",
            "http_status": 403,
            "retryable": False,
        }

    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"}, method="gcash", probe=fake
    )
    assert result["payment_methods"][0]["http_status"] == 403
    assert result["payment_methods"][0]["retryable"] is False
    assert result["status_code"] == 403


def test_an_eligible_account_reports_no_error_from_a_derived_row():
    fake, _ = _probe_map({"grabpay": dict(ELIGIBLE_RESULT, payment_method_types=["grabpay"])})
    result = pq.check_payment_qualification(
        {"access_token": "AT", "registration_country": "PH"},
        method="grabpay,direct_card",
        probe=fake,
    )
    assert result["payment_qualification_state"] == QUALIFICATION_ELIGIBLE
    assert result["error"] == ""
    assert result["status_code"] == 0
