"""Behaviour tests for ``sms_tool/paypal/flow_steps.py`` (round-5 audit target).

Why this file exists
--------------------
``flow_steps`` owns the gates that stand between the automation and a real
charge: the human-verification gate, the SMS/OTP gate, the submit click and the
redirect wait.  Its failure vocabulary is deliberately narrow — a
``_PayPalStepError`` with a ``step`` and a ``detail`` — so the tests here pin
*which* error escapes *when*, because that is the only signal the orchestrator
has to decide whether to fall back to another engine or to write a failed
session to disk.

Timers are the interesting part: the gate loop and the redirect wait both poll
``time.time()``, so they are driven with a controllable clock rather than real
sleeps.  No network, no browser.
"""

from __future__ import annotations

import pytest

from paypal_dom_fakes import FakeElement, FakePage
from sms_tool.paypal import flow_steps
from sms_tool.paypal.errors import _PayPalStepError


# ─────────────────────────────── helpers ─────────────────────────────────────


class Clock:
    """A manually advanced stand-in for time.time()."""

    def __init__(self, start=1_000_000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(flow_steps.time, "time", clk.time)
    monkeypatch.setattr(flow_steps.time, "sleep", clk.sleep)
    return clk


@pytest.fixture
def dead_page():
    return FakePage()


@pytest.fixture(autouse=True)
def _no_screenshots(monkeypatch):
    monkeypatch.setattr(flow_steps, "_screenshot", lambda *a, **k: None)


SMS_CFG = {"api_url": "https://sms.example/api", "phone": "5550100134",
           "timeout": 60, "poll_interval": 3}


# ─────────────────────── _is_human_verification_page ─────────────────────────


def test_is_human_verification_page_detects_a_captcha_iframe():
    page = FakePage(locators={'iframe[src*="captcha"]': FakeElement(visible=True)})
    assert flow_steps._is_human_verification_page(page) is True


def test_is_human_verification_page_detects_the_visible_challenge_text():
    page = FakePage(locators={'text="Confirm you\'re human"': FakeElement(visible=True)})
    assert flow_steps._is_human_verification_page(page) is True


def _body_page(text):
    """A page whose only content is a <body> reporting *text*."""
    body = FakeElement(visible=False)
    body.inner_text = lambda timeout=None: text
    return FakePage(locators={"body": body})


def test_is_human_verification_page_falls_back_to_scanning_the_body_text():
    page = _body_page("Please enable JS and disable any ad blocker")
    assert flow_steps._is_human_verification_page(page) is True


def test_is_human_verification_page_returns_false_for_a_normal_page(dead_page):
    assert flow_steps._is_human_verification_page(dead_page) is False


def test_is_human_verification_page_returns_false_for_unrelated_body_text():
    assert flow_steps._is_human_verification_page(_body_page("Pay with PayPal")) is False


def test_is_human_verification_page_swallows_a_failing_body_read():
    body = FakeElement(visible=False)
    body.inner_text = lambda timeout=None: (_ for _ in ()).throw(RuntimeError("boom"))
    page = FakePage(locators={"body": body})
    assert flow_steps._is_human_verification_page(page) is False


def test_is_human_verification_page_is_case_insensitive_on_the_body_text():
    """The body scan lowercases before matching, so casing does not matter."""
    assert flow_steps._is_human_verification_page(_body_page("CONFIRM YOU ARE HUMAN")) is True


def test_is_human_verification_page_detects_the_slider_challenge_text():
    page = _body_page("Move the slider all the way to the right")
    assert flow_steps._is_human_verification_page(page) is True


def test_is_human_verification_page_is_idempotent(dead_page):
    assert [flow_steps._is_human_verification_page(dead_page) for _ in range(3)] == [False] * 3


# ──────────────────── _handle_human_verification_gate ────────────────────────


def test_human_verification_gate_is_a_no_op_when_the_page_is_clean(dead_page):
    assert flow_steps._handle_human_verification_gate(
        dead_page, {}, "", False, "human_verification") is None


def test_human_verification_gate_raises_immediately_when_manual_is_not_allowed(monkeypatch, dead_page):
    monkeypatch.setattr(flow_steps, "_is_human_verification_page", lambda page: True)
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._handle_human_verification_gate(
            dead_page, {}, "", False, "human_verification")
    assert exc.value.step == "human_verification"
    assert exc.value.detail == "paypal_human_verification_required"


def test_human_verification_gate_propagates_the_caller_step_name(monkeypatch, dead_page):
    monkeypatch.setattr(flow_steps, "_is_human_verification_page", lambda page: True)
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._handle_human_verification_gate(dead_page, {}, "", False, "create_account")
    assert exc.value.step == "create_account"


def test_human_verification_gate_waits_and_returns_when_the_challenge_clears(monkeypatch, dead_page, clock):
    states = iter([True, True, False])
    monkeypatch.setattr(flow_steps, "_is_human_verification_page", lambda page: next(states))
    flow_steps._handle_human_verification_gate(
        dead_page, {"manual_human_verification": True}, "", False, "human_verification")
    assert clock.sleeps  # some waiting actually happened


def test_human_verification_gate_raises_after_the_timeout_expires(monkeypatch, dead_page, clock):
    """The deadline is honoured instead of looping forever."""
    monkeypatch.setattr(flow_steps, "_is_human_verification_page", lambda page: True)
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._handle_human_verification_gate(
            dead_page, {"manual_human_verification": True, "human_verification_timeout": 6},
            "", False, "human_verification")
    assert exc.value.detail == "paypal_human_verification_required"
    assert sum(clock.sleeps) >= 6


def test_human_verification_gate_defaults_to_a_300_second_window(monkeypatch, dead_page, clock):
    monkeypatch.setattr(flow_steps, "_is_human_verification_page", lambda page: True)
    with pytest.raises(_PayPalStepError):
        flow_steps._handle_human_verification_gate(
            dead_page, {"manual_human_verification": True}, "", False, "human_verification")
    assert sum(clock.sleeps) >= 300


def test_human_verification_gate_treats_a_blank_timeout_as_300(monkeypatch, dead_page, clock):
    """``int(x or 300)`` - a 0 or "" timeout must not shrink to a zero window."""
    monkeypatch.setattr(flow_steps, "_is_human_verification_page", lambda page: True)
    with pytest.raises(_PayPalStepError):
        flow_steps._handle_human_verification_gate(
            dead_page, {"manual_human_verification": True, "human_verification_timeout": 0},
            "", False, "human_verification")
    assert sum(clock.sleeps) >= 300


def test_human_verification_gate_swallows_a_failing_networkidle_wait(monkeypatch, dead_page):
    states = iter([True, False])
    monkeypatch.setattr(flow_steps, "_is_human_verification_page", lambda page: next(states))

    class Page(FakePage):
        def wait_for_load_state(self, state, timeout=None):
            raise RuntimeError("networkidle boom")

    flow_steps._handle_human_verification_gate(
        Page(), {"manual_human_verification": True}, "", False, "human_verification")


# ───────────────────────── _handle_sms_verification ──────────────────────────


def test_handle_sms_verification_returns_none_when_no_code_field_is_present(dead_page, clock):
    assert flow_steps._handle_sms_verification(dead_page, SMS_CFG, "baseline") is None


def test_handle_sms_verification_clicks_send_when_the_code_field_is_not_yet_visible(monkeypatch, dead_page, clock):
    """When no OTP box exists it first asks PayPal to send one."""
    clicked = []
    monkeypatch.setattr(flow_steps, "_click_with_fallback",
                        lambda page, selectors, timeout=0: clicked.append(selectors[0]) or True)
    flow_steps._handle_sms_verification(dead_page, SMS_CFG, "baseline")
    assert clicked == ['button:has-text("Send Code")']


def test_handle_sms_verification_raises_when_the_code_never_arrives(monkeypatch, dead_page, clock):
    page = FakePage(locators={'input[name="code"]': FakeElement(visible=True)})
    monkeypatch.setattr(flow_steps, "_poll_sms_code", lambda *a, **k: "")
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._handle_sms_verification(page, SMS_CFG, "baseline")
    assert exc.value.step == "sms_verify"
    assert exc.value.detail == "sms_code_timeout"


def test_handle_sms_verification_fills_the_code_and_confirms(monkeypatch, clock):
    el = FakeElement(visible=True, value="")
    page = FakePage(locators={'input[name="code"]': el})
    monkeypatch.setattr(flow_steps, "_poll_sms_code", lambda *a, **k: "123456")
    confirmed = {}
    monkeypatch.setattr(flow_steps, "_click_with_fallback",
                        lambda p, selectors, timeout=0: confirmed.update(n=len(selectors)) or True)

    assert flow_steps._handle_sms_verification(page, SMS_CFG, "baseline") == "123456"
    assert el.value == "123456"
    assert confirmed == {"n": 4}


def test_handle_sms_verification_passes_the_poll_settings_through(monkeypatch, clock):
    page = FakePage(locators={'input[name="code"]': FakeElement(visible=True)})
    seen = {}

    def _poll(api_url, baseline, timeout=None, poll_interval=None):
        seen.update(api_url=api_url, baseline=baseline,
                    timeout=timeout, poll_interval=poll_interval)
        return "654321"

    monkeypatch.setattr(flow_steps, "_poll_sms_code", _poll)
    flow_steps._handle_sms_verification(page, SMS_CFG, "BASE")
    assert seen == {"api_url": SMS_CFG["api_url"], "baseline": "BASE",
                    "timeout": 60, "poll_interval": 3}


def test_handle_sms_verification_stops_filling_after_the_first_visible_code_box(monkeypatch, clock):
    first, second = FakeElement(visible=True, value=""), FakeElement(visible=True, value="")
    page = FakePage(locators={'input[name="code"]': first, '#code': second})
    monkeypatch.setattr(flow_steps, "_poll_sms_code", lambda *a, **k: "111111")
    monkeypatch.setattr(flow_steps, "_click_with_fallback", lambda *a, **k: True)
    flow_steps._handle_sms_verification(page, SMS_CFG, "b")
    assert first.value == "111111"
    assert second.value == ""


def test_handle_sms_verification_swallows_a_failing_fill(monkeypatch, clock):
    page = FakePage(locators={'input[name="code"]': FakeElement(visible=True, fail={"fill"})})
    monkeypatch.setattr(flow_steps, "_poll_sms_code", lambda *a, **k: "123456")
    monkeypatch.setattr(flow_steps, "_click_with_fallback", lambda *a, **k: True)
    assert flow_steps._handle_sms_verification(page, SMS_CFG, "b") == "123456"


# ─────────────────────────────── _submit_payment ─────────────────────────────


def test_submit_payment_raises_when_no_submit_button_is_found(dead_page, clock):
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._submit_payment(dead_page)
    assert exc.value.step == "submit"
    assert exc.value.detail == "submit button not found"


def test_submit_payment_returns_none_after_a_successful_click(monkeypatch, dead_page, clock):
    seen = {}

    def _click(page, selectors, timeout=0):
        seen["n"] = len(selectors)
        seen["t"] = timeout
        return True

    monkeypatch.setattr(flow_steps, "_click_with_fallback", _click)
    assert flow_steps._submit_payment(dead_page) is None
    assert seen == {"n": 9, "t": 10000}


def test_submit_payment_waits_three_seconds_after_clicking(monkeypatch, dead_page, clock):
    monkeypatch.setattr(flow_steps, "_click_with_fallback", lambda *a, **k: True)
    flow_steps._submit_payment(dead_page)
    assert clock.sleeps == [3]


# ──────────────────────────── _wait_for_stripe_redirect ──────────────────────


class UrlPage(FakePage):
    def __init__(self, urls):
        super().__init__()
        self._urls = list(urls)
        self.url = self._urls[0]

    def advance(self):
        if len(self._urls) > 1:
            self._urls.pop(0)
            self.url = self._urls[0]


@pytest.fixture
def url_clock(monkeypatch):
    clk = Clock()

    def _sleep(secs):
        clk.sleeps.append(secs)
        clk.now += secs

    monkeypatch.setattr(flow_steps.time, "time", clk.time)
    monkeypatch.setattr(flow_steps.time, "sleep", _sleep)
    return clk


def test_wait_for_stripe_redirect_accepts_a_stripe_checkout_url():
    page = UrlPage(["https://checkout.stripe.com/c/pay/cs_1"])
    assert flow_steps._wait_for_stripe_redirect(page, timeout=10) is None


def test_wait_for_stripe_redirect_accepts_a_chatgpt_url():
    page = UrlPage(["https://chatgpt.com/"])
    assert flow_steps._wait_for_stripe_redirect(page, timeout=10) is None


def test_wait_for_stripe_redirect_raises_after_the_timeout(url_clock):
    page = UrlPage(["https://www.paypal.com/checkout"])
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._wait_for_stripe_redirect(page, timeout=6)
    assert exc.value.step == "wait_redirect"
    assert "redirect timeout" in exc.value.detail
    assert sum(url_clock.sleeps) >= 6


def test_wait_for_stripe_redirect_error_message_includes_the_stuck_url(url_clock):
    page = UrlPage(["https://www.paypal.com/checkout?token=abc"])
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._wait_for_stripe_redirect(page, timeout=4)
    assert "paypal.com" in exc.value.detail


def test_wait_for_stripe_redirect_truncates_the_url_in_the_error(url_clock):
    long_url = "https://www.paypal.com/checkout?" + "x" * 300
    page = UrlPage([long_url])
    with pytest.raises(_PayPalStepError) as exc:
        flow_steps._wait_for_stripe_redirect(page, timeout=4)
    assert "x" * 81 not in exc.value.detail


def test_wait_for_stripe_redirect_polls_until_the_url_changes(url_clock):
    page = UrlPage(["https://www.paypal.com/a", "https://www.paypal.com/b",
                    "https://checkout.stripe.com/c/pay/cs_1"])

    original_sleep = flow_steps.time.sleep

    def _advancing_sleep(secs):
        original_sleep(secs)
        page.advance()

    flow_steps.time.sleep = _advancing_sleep
    assert flow_steps._wait_for_stripe_redirect(page, timeout=30) is None


# ───────────────────── _prepare_openai_checkout_paypal ───────────────────────


def test_prepare_openai_checkout_returns_false_for_a_non_openai_url(dead_page):
    dead_page.url = "https://www.paypal.com/checkout"
    flow_steps._is_openai_checkout_url = lambda url: False
    assert flow_steps._prepare_openai_checkout_paypal(
        dead_page, address={}, first_name="A", last_name="B", phone="",
        debug_dir="", debug_enabled=False) is False


def test_prepare_openai_checkout_returns_true_as_soon_as_the_url_is_paypal(dead_page):
    dead_page.url = "https://www.paypal.com/checkout"
    flow_steps._is_openai_checkout_url = lambda url: True
    flow_steps._is_paypal_url = lambda url: True
    assert flow_steps._prepare_openai_checkout_paypal(
        dead_page, address={}, first_name="A", last_name="B", phone="",
        debug_dir="", debug_enabled=False) is True


def test_prepare_openai_checkout_fills_billing_and_selects_paypal(dead_page, clock):
    """One full pass: billing -> select PayPal -> continue -> still OpenAI."""
    calls = []
    dead_page.url = "https://checkout.openai.com/c/pay"
    flow_steps._is_openai_checkout_url = lambda url: True
    flow_steps._is_paypal_url = lambda url: False
    flow_steps._fill_openai_checkout_billing = lambda *a, **k: calls.append("billing")
    flow_steps._select_openai_checkout_paypal = lambda page: calls.append("select") or True
    flow_steps._click_openai_checkout_continue = lambda page: calls.append("continue") or False

    # stop after one iteration
    def _stopping_sleep(secs):
        clock.sleeps.append(secs)
        clock.now += 45

    flow_steps.time.sleep = _stopping_sleep

    flow_steps._prepare_openai_checkout_paypal(
        dead_page, address={}, first_name="A", last_name="B", phone="",
        debug_dir="", debug_enabled=False)
    assert calls == ["billing", "select", "continue"]


def test_prepare_openai_checkout_swallows_a_failing_load_state_wait(dead_page, clock):
    class Page(FakePage):
        url = "https://checkout.openai.com/c/pay"

        def wait_for_load_state(self, state, timeout=None):
            raise RuntimeError("boom")

    flow_steps._is_openai_checkout_url = lambda url: True
    flow_steps._is_paypal_url = lambda url: False
    flow_steps._fill_openai_checkout_billing = lambda *a, **k: None
    flow_steps._select_openai_checkout_paypal = lambda page: False
    flow_steps._click_openai_checkout_continue = lambda page: False

    def _stopping_sleep(secs):
        clock.now += 45

    flow_steps.time.sleep = lambda secs: _stopping_sleep(secs)
    assert flow_steps._prepare_openai_checkout_paypal(
        Page(), address={}, first_name="A", last_name="B", phone="",
        debug_dir="", debug_enabled=False) is False


def test_prepare_openai_checkout_keeps_retrying_within_its_45_second_budget(dead_page, clock):
    """Each loop iteration fills billing again; the budget bounds the retries."""
    fill_calls = []
    dead_page.url = "https://checkout.openai.com/c/pay"
    flow_steps._is_openai_checkout_url = lambda url: True
    flow_steps._is_paypal_url = lambda url: False
    flow_steps._fill_openai_checkout_billing = lambda *a, **k: fill_calls.append(1)
    flow_steps._select_openai_checkout_paypal = lambda page: False
    flow_steps._click_openai_checkout_continue = lambda page: False

    result = flow_steps._prepare_openai_checkout_paypal(
        dead_page, address={}, first_name="A", last_name="B", phone="",
        debug_dir="", debug_enabled=False)

    assert result is False
    assert len(fill_calls) >= 45  # one per second of the 45s budget
    assert sum(clock.sleeps) >= 45


def test_prepare_openai_checkout_returns_false_once_the_budget_is_spent_on_a_non_paypal_url(dead_page, clock):
    dead_page.url = "https://example.com/somewhere"
    flow_steps._is_openai_checkout_url = lambda url: False
    flow_steps._is_paypal_url = lambda url: False
    assert flow_steps._prepare_openai_checkout_paypal(
        dead_page, address={}, first_name="A", last_name="B", phone="",
        debug_dir="", debug_enabled=False) is False
