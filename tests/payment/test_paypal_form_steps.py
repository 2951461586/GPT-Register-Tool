"""Behaviour tests for ``sms_tool/paypal/form_steps.py`` (round-5 audit target).

Why this file exists
--------------------
``form_steps`` is the layer that gives meaning to the raw DOM primitives: it
owns the selector lists for email / name / card / billing address and decides
what counts as "this field is filled".  It is also where two distinct classes of
defect showed up during this pass:

1. **Unbound globals (FIXED 2026-09-02).** The module called ``random.uniform``
   and ``re.sub`` but imported neither, so five entry points raised ``NameError``
   the moment they reached those lines.  Because every caller wraps the fill in
   a ``try/except``, the production symptom was not a crash — it was a step that
   silently aborted halfway through a payment.  The imports were added and the
   tests below now assert the *working* behaviour, with one guard
   (``test_form_steps_imports_random_and_re``) left behind so the imports cannot
   be dropped again.

2. **Silent partial success.** ``_fill_billing_address`` / ``_fill_card`` print
   "[!] ... not found" and carry on, so a half-filled form is indistinguishable
   from a filled one at this layer.

Browser access is faked with the shared doubles in ``paypal_dom_fakes``; the
real production functions run unchanged.  No network, no browser.
"""

from __future__ import annotations

import pytest

from paypal_dom_fakes import FakeElement, FakePage
from sms_tool.paypal import form_steps
from sms_tool.paypal.errors import _PayPalStepError


# ─────────────────────────────── fixtures ────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """form_steps calls time.sleep() between every field; make it free."""
    sleeps = []
    monkeypatch.setattr(form_steps.time, "sleep", lambda secs: sleeps.append(secs))
    return sleeps


@pytest.fixture
def dead_page():
    """A page where no selector resolves to anything."""
    return FakePage()


@pytest.fixture
def live_page():
    """A page whose DOM-id fill always succeeds and echoes the value back."""
    return FakePage(evaluate=lambda script, arg: arg["value"])


ADDRESS = {"line1": "3110 Sunset Blvd", "city": "Los Angeles",
           "state": "CA", "postal_code": "90026", "country": "US",
           "first_name": "Ada", "last_name": "Lovelace"}


# ──────────────────────── the unbound-global defect ──────────────────────────


def test_form_steps_imports_random_and_re():
    """REGRESSION GUARD for a fixed bug.

    The module calls ``random.uniform`` (11x) and ``re.sub`` but until
    2026-09-02 imported neither, so five fill steps raised ``NameError``
    mid-payment. Callers wrap steps in a bare ``except``, so the symptom was a
    payment silently abandoned halfway - no crash, no log line.

    Asserting the imports exist is the cheap durable check: the expensive
    failure mode is invisible otherwise.
    """
    assert hasattr(form_steps, "random"), (
        "form_step calls random.uniform but does not import random - every fill "
        "step that reaches it raises NameError mid-payment"
    )
    assert hasattr(form_steps, "re"), (
        "form_step calls re.sub but does not import re - _fill_phone_if_present "
        "raises NameError mid-payment"
    )


@pytest.mark.parametrize("name", [
    "_fill_signup_name", "_fill_phone_if_present", "_fill_password",
    "_fill_card", "_fill_billing_address",
])
def test_steps_that_touch_random_or_re_complete_without_error(name, live_page):
    """The five entry points that touch `random`/`re` must finish the fill.

    These are the exact functions that raised ``NameError`` while the imports
    were missing. A page where the field *is* found must now be filled
    end-to-end rather than dying partway - a partial fill is indistinguishable
    from a complete one to every caller above this layer.
    """
    fn = getattr(form_steps, name)
    args = {
        "_fill_signup_name": (live_page, "Ada", "Lovelace"),
        "_fill_phone_if_present": (live_page, "+1 (555) 010-0134"),
        "_fill_password": (live_page, "pw"),
        "_fill_card": (live_page, {"number": "4242", "exp_month": "12",
                                   "exp_year": "2030", "cvv": "123"}),
        "_fill_billing_address": (live_page, dict(ADDRESS)),
    }[name]
    fn(*args)  # must not raise


def test_fill_signup_name_writes_both_names_not_just_the_first(live_page):
    """Guard against a half-applied form: firstName written, lastName skipped.

    While the imports were missing the DOM was mutated up to the crash point and
    no further, so a payment form could carry a first name and no last name.
    """
    form_steps._fill_signup_name(live_page, "Ada", "Lovelace")
    written = [arg["id"] for _script, arg in live_page.evaluate_calls]
    assert "firstName" in written
    assert "lastName" in written, (
        "form fill stopped after the first field - this is the half-applied "
        "payment form the missing imports used to cause"
    )


def test_fill_password_returns_none_without_error_when_no_field_is_found(dead_page):
    """By contrast, a dead page never reaches the unbound name: silent no-op."""
    assert form_steps._fill_password(dead_page, "pw") is None


# ───────────── intended behaviour, currently blocked by the defect ───────────


def test_fill_signup_name_reports_both_names_as_filled(capsys):
    page = FakePage(evaluate=lambda script, arg: arg["value"])
    form_steps._fill_signup_name(page, "Ada", "Lovelace")
    assert "Name filled: Ada Lovelace" in capsys.readouterr().out


def test_fill_phone_if_present_strips_a_us_country_code(capsys):
    page = FakePage(evaluate=lambda script, arg: arg["value"])
    form_steps._fill_phone_if_present(page, "+1 (555) 010-0134")
    assert "Phone filled: 5550100134" in capsys.readouterr().out


def test_fill_billing_address_redacts_nothing_but_logs_the_address(capsys):
    page = FakePage(evaluate=lambda script, arg: arg["value"])
    form_steps._fill_billing_address(page, dict(ADDRESS))
    assert "Address filled:" in capsys.readouterr().out


# ───────────────────────────── _ensure_country_us ────────────────────────────


def _country_page(current):
    """A page whose country <select> reports *current* (the JS snippet's output)."""
    el = FakeElement(visible=True, fail={"select_option"})
    el.evaluate = lambda script: current
    return FakePage(locators={'select[id="country"]': el})


def test_ensure_country_us_returns_false_when_the_select_is_already_us():
    assert form_steps._ensure_country_us(_country_page({"value": "us", "text": ""})) is False


def test_ensure_country_us_accepts_the_long_country_value():
    assert form_steps._ensure_country_us(
        _country_page({"value": "united states", "text": ""})) is False


def test_ensure_country_us_matches_the_visible_option_text():
    assert form_steps._ensure_country_us(
        _country_page({"value": "xyz", "text": "united states of america"})) is False


def test_ensure_country_us_comparison_relies_on_the_in_page_lowercasing(monkeypatch):
    """AUDIT POINT: the Python-side literals are lowercase and case-sensitive.

    The early return only fires because the injected JS lowercases both value and
    text.  If the page (or any shimmed ``evaluate``) preserves the original
    casing, an already-correct US selection is not recognised and the code
    re-selects it - a needless mutation on a live payment form.
    """
    called = []
    monkeypatch.setattr(form_steps, "_select_with_fallback",
                        lambda *a, **k: called.append(True) or False)

    # lowercased (what the real JS returns) -> recognised, no re-select
    form_steps._ensure_country_us(_country_page({"value": "us", "text": "united states"}))
    assert called == []

    # original casing -> NOT recognised, triggers a re-select
    form_steps._ensure_country_us(_country_page({"value": "US", "text": "United States"}))
    assert called == [True]


def test_ensure_country_us_returns_false_when_nothing_can_be_changed(monkeypatch, dead_page):
    """No select on the page and no successful select -> reported as 'no change'."""
    monkeypatch.setattr(form_steps, "_select_with_fallback", lambda *a, **k: False)
    assert form_steps._ensure_country_us(dead_page) is False


def test_ensure_country_us_reports_true_only_after_a_successful_select(monkeypatch):
    monkeypatch.setattr(form_steps, "_select_with_fallback", lambda *a, **k: True)
    assert form_steps._ensure_country_us(FakePage()) is True


def test_ensure_country_us_swallows_errors_and_still_offers_all_six_selectors(monkeypatch):
    el = FakeElement(visible=True)
    el.evaluate = lambda script: (_ for _ in ()).throw(RuntimeError("boom"))
    page = FakePage(locators={'select[id="country"]': el})
    offered = {}
    monkeypatch.setattr(form_steps, "_select_with_fallback",
                        lambda p, selectors, value, **k: offered.update(n=len(selectors),
                                                                        value=value) or False)
    assert form_steps._ensure_country_us(page) is False
    assert offered == {"n": 6, "value": "US"}


def test_ensure_country_us_tolerates_a_non_dict_evaluate_result(monkeypatch):
    page = _country_page(None)
    monkeypatch.setattr(form_steps, "_select_with_fallback", lambda *a, **k: False)
    assert form_steps._ensure_country_us(page) is False


# ────────────────────── OpenAI checkout page delegates ───────────────────────


def test_select_openai_checkout_paypal_delegates_to_click_with_fallback(monkeypatch):
    seen = {}

    def _fake(page, selectors, timeout=8000):
        seen["n"] = len(selectors)
        seen["timeout"] = timeout
        return True

    monkeypatch.setattr(form_steps, "_click_with_fallback", _fake)
    assert form_steps._select_openai_checkout_paypal(FakePage()) is True
    assert seen == {"n": 8, "timeout": 5000}


def test_click_openai_checkout_continue_delegates_with_a_longer_timeout(monkeypatch):
    seen = {}

    def _fake(page, selectors, timeout=0):
        seen["t"] = timeout
        seen["n"] = len(selectors)
        return True

    monkeypatch.setattr(form_steps, "_click_with_fallback", _fake)
    assert form_steps._click_openai_checkout_continue(FakePage()) is True
    assert seen == {"t": 8000, "n": 7}


def test_openai_checkout_helpers_propagate_a_false_result(monkeypatch):
    monkeypatch.setattr(form_steps, "_click_with_fallback", lambda *a, **k: False)
    assert form_steps._select_openai_checkout_paypal(FakePage()) is False
    assert form_steps._click_openai_checkout_continue(FakePage()) is False


# ────────────────────── _fill_openai_checkout_billing ────────────────────────


def test_fill_openai_checkout_billing_skips_blank_values(monkeypatch):
    calls = []
    monkeypatch.setattr(form_steps, "_fill_visible_input",
                        lambda page, sel, value, **k: calls.append(value) or False)
    monkeypatch.setattr(form_steps, "_fill_with_fallback",
                        lambda page, sel, value, **k: calls.append(value) or False)
    monkeypatch.setattr(form_steps, "_select_with_fallback", lambda *a, **k: True)

    form_steps._fill_openai_checkout_billing(
        FakePage(), {"country": "US", "line1": "", "city": ""}, "Ada", "", "")

    # Only the full name is attempted (by both strategies); every blank value is
    # skipped outright rather than being sent as an empty fill.
    assert set(calls) == {"Ada"}
    assert calls == ["Ada", "Ada"]


def test_fill_openai_checkout_billing_tries_both_fill_strategies_per_field(monkeypatch):
    order = []
    monkeypatch.setattr(form_steps, "_fill_visible_input",
                        lambda page, sel, value, **k: order.append("visible") or False)
    monkeypatch.setattr(form_steps, "_fill_with_fallback",
                        lambda page, sel, value, **k: order.append("fallback") or False)
    monkeypatch.setattr(form_steps, "_select_with_fallback", lambda *a, **k: True)

    form_steps._fill_openai_checkout_billing(FakePage(), {"country": "US"}, "Ada", "", "")
    assert order == ["visible", "fallback"]


def test_fill_openai_checkout_billing_stops_after_the_first_successful_strategy(monkeypatch):
    order = []
    monkeypatch.setattr(form_steps, "_fill_visible_input",
                        lambda page, sel, value, **k: order.append("visible") or True)
    monkeypatch.setattr(form_steps, "_fill_with_fallback",
                        lambda page, sel, value, **k: order.append("fallback") or True)
    monkeypatch.setattr(form_steps, "_select_with_fallback", lambda *a, **k: True)

    form_steps._fill_openai_checkout_billing(FakePage(), {"country": "US"}, "Ada", "", "")
    assert order == ["visible"]


def test_fill_openai_checkout_billing_always_selects_a_country(monkeypatch):
    seen = {}
    monkeypatch.setattr(form_steps, "_select_with_fallback",
                        lambda page, selectors, value, **k: seen.update(value=value) or True)
    form_steps._fill_openai_checkout_billing(FakePage(), {"country": "DE"}, "A", "B", "")
    assert seen["value"] == "DE"


def test_fill_openai_checkout_billing_prefers_postal_code_over_zip(monkeypatch):
    seen = []
    monkeypatch.setattr(form_steps, "_fill_visible_input",
                        lambda page, sel, value, **k: seen.append(value) or True)
    monkeypatch.setattr(form_steps, "_fill_with_fallback", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_select_with_fallback", lambda *a, **k: True)

    form_steps._fill_openai_checkout_billing(
        FakePage(), {"country": "US", "postal_code": "90026", "zip": "00000"}, "A", "", "")
    assert "90026" in seen and "00000" not in seen


# ─────────────────────────── _click_create_account ───────────────────────────


def test_click_create_account_tries_the_guest_card_path_first(monkeypatch):
    seen = []
    monkeypatch.setattr(form_steps, "_click_with_fallback",
                        lambda page, selectors, timeout=0: seen.append(selectors[0]) or True)
    form_steps._click_create_account(FakePage())
    assert seen == ['text="Pay with Debit or Credit Card"']


def test_click_create_account_falls_back_to_the_signup_link(capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(form_steps, "_click_with_fallback",
                        lambda page, selectors, timeout=0: seen.append(selectors[0]) or False)
    form_steps._click_create_account(FakePage())
    assert seen == ['text="Pay with Debit or Credit Card"', 'text="Create an account"']
    assert "already on form" in capsys.readouterr().out


def test_click_create_account_never_raises_when_the_page_has_no_buttons(capsys, dead_page):
    form_steps._click_create_account(dead_page)
    assert "already on form" in capsys.readouterr().out


# ──────────────────────────── _fill_signup_email ─────────────────────────────


def test_fill_signup_email_raises_a_step_error_when_no_email_field_exists(dead_page):
    with pytest.raises(_PayPalStepError) as exc:
        form_steps._fill_signup_email(dead_page, "buyer@example.com")
    assert exc.value.step == "fill_email"
    assert "not found" in exc.value.detail


def test_fill_signup_email_raises_when_the_field_stays_blank(monkeypatch, dead_page):
    """The fill helpers lie and say True; the verification catches it."""
    monkeypatch.setattr(form_steps, "_ensure_country_us", lambda page: False)
    monkeypatch.setattr(form_steps, "_fill_dom_ids", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_visible_field_has_value", lambda *a, **k: False)

    with pytest.raises(_PayPalStepError) as exc:
        form_steps._fill_signup_email(dead_page, "buyer@example.com")
    assert "stayed blank" in exc.value.detail


def test_fill_signup_email_returns_cleanly_when_the_field_verifies(monkeypatch, dead_page, capsys):
    monkeypatch.setattr(form_steps, "_ensure_country_us", lambda page: False)
    monkeypatch.setattr(form_steps, "_fill_dom_ids", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_visible_field_has_value", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_click_with_fallback", lambda *a, **k: False)

    form_steps._fill_signup_email(dead_page, "buyer@example.com")
    assert "Email filled: buyer@example.com" in capsys.readouterr().out


def test_fill_signup_email_waits_for_the_checkout_form_after_continue(monkeypatch, dead_page):
    monkeypatch.setattr(form_steps, "_ensure_country_us", lambda page: False)
    monkeypatch.setattr(form_steps, "_fill_dom_ids", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_visible_field_has_value", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_click_with_fallback", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_wait_for_checkout_form_after_email", lambda page: False)

    with pytest.raises(_PayPalStepError) as exc:
        form_steps._fill_signup_email(dead_page, "buyer@example.com")
    assert "checkout form did not appear" in exc.value.detail


def test_fill_signup_email_refills_the_email_if_it_is_lost_after_continue(monkeypatch, dead_page):
    """Email survives the continue click -> no refill; lost -> one refill."""
    monkeypatch.setattr(form_steps, "_ensure_country_us", lambda page: False)
    monkeypatch.setattr(form_steps, "_fill_dom_ids", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_click_with_fallback", lambda *a, **k: True)
    monkeypatch.setattr(form_steps, "_wait_for_checkout_form_after_email", lambda page: True)

    refills = []
    monkeypatch.setattr(form_steps, "_fill_visible_input",
                        lambda page, sel, value, **k: refills.append(value) or True)

    # case A: still filled after continue -> no refill
    monkeypatch.setattr(form_steps, "_visible_field_has_value", lambda *a, **k: True)
    form_steps._fill_signup_email(dead_page, "buyer@example.com")
    assert refills == []

    # case B: lost after continue -> refill once
    values = iter([True, False])
    monkeypatch.setattr(form_steps, "_visible_field_has_value", lambda *a, **k: next(values))
    form_steps._fill_signup_email(dead_page, "buyer@example.com")
    assert refills == ["buyer@example.com"]


# ─────────────────────────── _verify_checkout_fields ─────────────────────────


def _page_with_all_fields_present():
    return FakePage(evaluate=lambda script, arg: {
        "email": "a@b.com", "phone": "555", "cardNumber": "4242", "cardExpiry": "12/30",
        "cardCvv": "123", "firstName": "Ada", "lastName": "Lovelace",
        "billingLine1": "1 Main", "billingLocality": "LA", "billingPostalCode": "90026",
        "password": "pw",
    }.get(arg, None))


def test_verify_checkout_fields_passes_when_every_required_field_has_a_value(capsys):
    form_steps._verify_checkout_fields(_page_with_all_fields_present())
    assert "PayPal fields verified" in capsys.readouterr().out


def test_verify_checkout_fields_redacts_the_card_number_cvv_and_password(capsys):
    form_steps._verify_checkout_fields(_page_with_all_fields_present())
    out = capsys.readouterr().out
    assert "[REDACTED]" in out
    assert "4242" not in out
    assert "123" not in out


def test_verify_checkout_fields_raises_listing_every_blank_required_field():
    page = FakePage(evaluate=lambda script, arg: None)
    with pytest.raises(_PayPalStepError) as exc:
        form_steps._verify_checkout_fields(page)
    detail = exc.value.detail
    for field in ("email", "cardNumber", "cardExpiry", "cardCvv", "firstName",
                  "lastName", "billingLine1", "billingCity", "billingPostalCode",
                  "password"):
        assert field in detail


def test_verify_checkout_fields_does_not_require_a_phone_number():
    """Phone is read but deliberately excluded from the `required` list."""
    page = FakePage(evaluate=lambda script, arg: None if arg == "phone" else "filled")
    form_steps._verify_checkout_fields(page)  # must not raise


def test_verify_checkout_fields_falls_back_to_selectors_when_ids_are_blank():
    page = FakePage(
        locators={sel: FakeElement(visible=True, value="filled") for sel in (
            'input[id="email"]', 'input[id="cardNumber"]', 'input[id="cardExpiry"]',
            'input[id="cardCvv"]', 'input[id="firstName"]', 'input[id="lastName"]',
            'input[id="billingLine1"]', 'input[id="billingCity"]',
            'input[id="billingPostalCode"]', 'input[id="password"]')},
        evaluate=lambda script, arg: None,
    )
    form_steps._verify_checkout_fields(page)  # must not raise


def test_verify_checkout_fields_is_idempotent(capsys):
    page = _page_with_all_fields_present()
    form_steps._verify_checkout_fields(page)
    form_steps._verify_checkout_fields(page)
    assert capsys.readouterr().out.count("PayPal fields verified") == 2


# ─────────────────────────────── _accept_terms ───────────────────────────────


def test_accept_terms_checks_the_first_visible_unchecked_box():
    el = FakeElement(visible=True)
    page = FakePage(locators={'input[type="checkbox"][name*="agree"]': el})
    form_steps._accept_terms(page)
    assert ("check",) in el.record


def test_accept_terms_stops_after_the_first_match():
    first, second = FakeElement(visible=True), FakeElement(visible=True)
    page = FakePage(locators={'input[type="checkbox"][name*="agree"]': first,
                              'input[type="checkbox"][name*="terms"]': second})
    form_steps._accept_terms(page)
    assert first.record and second.record == []


def test_accept_terms_is_a_no_op_when_no_checkbox_exists(dead_page):
    form_steps._accept_terms(dead_page)  # must not raise


def test_accept_terms_swallows_errors_from_a_broken_checkbox():
    el = FakeElement(visible=True, fail={"check"})
    page = FakePage(locators={'input[type="checkbox"][name*="agree"]': el})
    form_steps._accept_terms(page)  # must not raise


def test_accept_terms_skips_an_already_checked_box():
    el = FakeElement(visible=True)
    el.is_checked = lambda: True
    page = FakePage(locators={'input[type="checkbox"][name*="agree"]': el})
    form_steps._accept_terms(page)
    assert ("check",) not in el.record
