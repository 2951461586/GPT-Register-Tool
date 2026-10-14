"""Tests for the UPI deliverable verification gate (step 8).

Ported 2026-09-30 from the reference ``upi-zero-link/upi_zero_link/verify.py``.
The gate exists because ``hosted_instructions_url`` is returned even when the
setup_intent was **declined**, so "there is a URL" is not proof of a ₹0 mandate.

Also pins the browser-rail default flip: the headless-Chromium rail is
Cloudflare-blocked and CSP-blocked, so it must be OFF by default.
"""

from __future__ import annotations

import base64
import json

from sms_tool.upi_link import verify as V
from sms_tool.upi_link.pipeline import _resolve_upi_runtime

INSTRUCTIONS_URL = "https://payments.stripe.com/upi/instructions/abc123"


def _payload_b64(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _page(payload: dict, *, attr_order: str = "id_first") -> str:
    b64 = _payload_b64(payload)
    if attr_order == "id_first":
        meta = f'<meta id="payload" data-message="{b64}">'
    else:
        meta = f'<meta data-message="{b64}" id="payload">'
    return f"<html><head>{meta}</head><body></body></html>"


# --------------------------------------------------------------------------
# decode
# --------------------------------------------------------------------------


def test_decodes_meta_payload_both_attribute_orders():
    payload = {"intent_state": "requires_action", "mobile_auth_url": "upi://mandate?fam=1.00"}
    assert V.decode_instructions_payload(_page(payload)) == payload
    assert V.decode_instructions_payload(_page(payload, attr_order="message_first")) == payload


def test_missing_or_broken_meta_is_none():
    assert V.decode_instructions_payload("<html><head></head></html>") is None
    assert V.decode_instructions_payload('<meta id="payload" data-message="!!!not-base64!!!">') is None
    assert V.decode_instructions_payload("") is None


# --------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------


def test_signed_mandate_passes():
    ok, label = V.judge_instructions_payload(
        {"intent_state": "requires_action", "mobile_auth_url": "upi://mandate?ver=01&fam=1.00&am=1999.00"}
    )
    assert ok is True and "requires_action" in label and "fam=1.00" in label


def test_declined_mandate_fails_even_though_a_url_exists():
    """``requires_payment_method`` + fam=1999.00 is the fake-link shape."""
    ok, label = V.judge_instructions_payload(
        {"intent_state": "requires_payment_method", "mobile_auth_url": "upi://mandate?fam=1999.00"}
    )
    assert ok is False and "requires_payment_method" in label


def test_1999_fam_fails_even_when_state_is_requires_action():
    """fam=1999.00 identifies the ₹1999 payment chain, not the ₹0 mandate."""
    ok, _ = V.judge_instructions_payload(
        {"intent_state": "requires_action", "mobile_auth_url": "upi://mandate?fam=1999.00"}
    )
    assert ok is False


def test_am_1999_alone_does_not_fail():
    """``am=1999.00`` is the AUTHORISATION CAP (amrule=MAX), not the charge."""
    ok, _ = V.judge_instructions_payload(
        {"intent_state": "processing", "mobile_auth_url": "upi://mandate?am=1999.00&amrule=MAX&fam=1.00"}
    )
    assert ok is True


def test_cancelled_and_succeeded_do_not_pass():
    for state in ("canceled", "cancelled", "succeeded", ""):
        ok, _ = V.judge_instructions_payload({"intent_state": state, "mobile_auth_url": "upi://x?fam=1.00"})
        assert ok is False, state


def test_processing_state_with_uri_passes():
    ok, _ = V.judge_instructions_payload({"upi_uri": "upi://mandate?fam=1.00", "intent_state": "processing"})
    assert ok is True


# --------------------------------------------------------------------------
# verify_instructions_url
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text


def _session_factory_factory(make):
    """Build a ``session_factory(proxy)`` whose ``.get`` returns ``make()``."""

    class _Session:
        def get(self, url, **kwargs):
            return make()

    return lambda proxy: _Session()


def test_verify_url_empty_is_a_definite_no():
    assert V.verify_instructions_url("", attempts=1) == (False, "empty_url")


def test_verify_url_good_page_passes():
    session_factory = _session_factory_factory(
        lambda: _Resp(200, _page({"intent_state": "requires_action", "mobile_auth_url": "upi://x?fam=1.00"}))
    )
    ok, label = V.verify_instructions_url(INSTRUCTIONS_URL, session_factory=session_factory, attempts=1)
    assert ok is True and "requires_action" in label


def test_verify_url_declined_page_is_a_definite_no():
    session_factory = _session_factory_factory(
        lambda: _Resp(200, _page({"intent_state": "requires_payment_method", "mobile_auth_url": "upi://x?fam=1999.00"}))
    )
    ok, label = V.verify_instructions_url(INSTRUCTIONS_URL, session_factory=session_factory, attempts=1)
    assert ok is False and "requires_payment_method" in label


def test_verify_url_4xx_is_definite_and_not_retried():
    calls = {"n": 0}

    def make():
        calls["n"] += 1
        return _Resp(404, "")

    ok, label = V.verify_instructions_url(INSTRUCTIONS_URL, session_factory=_session_factory_factory(make), attempts=3)
    assert (ok, label) == (False, "http_404")
    assert calls["n"] == 1, "a 4xx is a definite verdict; it must not be retried"


def test_verify_url_unreachable_is_inconclusive():
    def boom():
        raise RuntimeError("network down")

    calls = {"n": 0}

    class _Session:
        def get(self, url, **kwargs):
            calls["n"] += 1
            return boom()

    ok, label = V.verify_instructions_url(INSTRUCTIONS_URL, session_factory=lambda proxy: _Session(), attempts=2)
    assert ok is False and label == "unreachable"
    assert label in V.INCONCLUSIVE
    assert calls["n"] >= 2, "an unreachable page is retried (idempotent read)"


def test_verify_url_no_payload_is_inconclusive_not_fake():
    session_factory = _session_factory_factory(lambda: _Resp(200, "<html><body>no meta here</body></html>"))
    ok, label = V.verify_instructions_url(INSTRUCTIONS_URL, session_factory=session_factory, attempts=1)
    assert ok is False and label == "no_payload" and label in V.INCONCLUSIVE


# --------------------------------------------------------------------------
# verdict subdivision: mandate_not_signed != wrong link
# --------------------------------------------------------------------------


def test_classify_separates_mandate_not_signed_from_wrong_chain():
    """``requires_payment_method`` (no mandate) and ``fam=1999`` (wrong link) differ.

    On the ``cs_`` rail the mandate cannot be signed third-party (Stripe refuses
    it), so the two must not collapse into one "link_unverified" verdict.
    """
    mandate_ok, mandate_label, mandate_verdict = V.classify_instructions_payload(
        {"intent_state": "requires_payment_method", "mobile_auth_url": "upi://mandate?fam=1999.00"}
    )
    assert mandate_ok is False
    assert mandate_verdict == V.VERDICT_MANDATE_NOT_SIGNED
    assert "requires_payment_method" in mandate_label

    chain_ok, _, chain_verdict = V.classify_instructions_payload(
        {"intent_state": "requires_action", "mobile_auth_url": "upi://mandate?fam=1999.00"}
    )
    assert chain_ok is False and chain_verdict == V.VERDICT_PAYMENT_CHAIN

    ok, _, ok_verdict = V.classify_instructions_payload(
        {"intent_state": "processing", "mobile_auth_url": "upi://mandate?fam=1.00"}
    )
    assert ok is True and ok_verdict == V.VERDICT_OK


def test_verify_verdict_returns_the_reason_code():
    page = _page({"intent_state": "requires_payment_method", "mobile_auth_url": "upi://x?fam=1999.00"})
    session_factory = _session_factory_factory(lambda: _Resp(200, page))
    ok, label, verdict = V.verify_instructions_verdict(INSTRUCTIONS_URL, session_factory=session_factory, attempts=1)
    assert ok is False and verdict == V.VERDICT_MANDATE_NOT_SIGNED and "requires_payment_method" in label


def test_verify_5xx_is_inconclusive_not_a_fake_link():
    """A 5xx read failure is not evidence of a bad link, so it must stay inconclusive."""
    session_factory = _session_factory_factory(lambda: _Resp(503, ""))
    ok, label, verdict = V.verify_instructions_verdict(INSTRUCTIONS_URL, session_factory=session_factory, attempts=1)
    assert ok is False
    assert verdict == "http_5xx" and verdict in V.INCONCLUSIVE
    assert V.verify_instructions_url(INSTRUCTIONS_URL, session_factory=session_factory, attempts=1) == (
        False,
        "http_5xx",
    )


def test_unverified_contract_maps_three_distinct_reasons():
    from sms_tool.upi_link.pipeline import _upi_link_unverified_contract

    mandate = _upi_link_unverified_contract("mandate_not_signed", "state=requires_payment_method fam=1999.00")
    assert mandate["error_code"] == "mandate_not_signed"
    assert mandate["error_stage"] == "mandate"
    assert mandate["retryable"] is True
    assert "不是废链" in mandate["error"]

    chain = _upi_link_unverified_contract("payment_chain", "state=requires_action fam=1999.00")
    assert chain["error_code"] == "payment_chain_link" and chain["retryable"] is False

    other = _upi_link_unverified_contract("not_authorized", "state=canceled fam=?")
    assert other["error_code"] == "link_unverified" and other["retryable"] is False


# --------------------------------------------------------------------------
# item 1: browser rail default
# --------------------------------------------------------------------------


def test_browser_rail_is_off_by_default():
    """The headless rail is CF-blocked + CSP-blocked; it must default OFF."""
    runtime = _resolve_upi_runtime(
        access_token="t",
        proxy="http://p:1",
        checkout_proxy="",
        provider_proxy="",
        approve_proxy="",
        target_country="",
        checkout_country="",
        payment_country="",
        require_zero=None,
        runtime_config={"upi": {}},
        device_id="",
        session_token="",
    )
    assert runtime.browser_rail is False


def test_browser_rail_can_be_forced_on_via_config():
    runtime = _resolve_upi_runtime(
        access_token="t",
        proxy="http://p:1",
        checkout_proxy="",
        provider_proxy="",
        approve_proxy="",
        target_country="",
        checkout_country="",
        payment_country="",
        require_zero=None,
        runtime_config={"upi": {"browser_rail": True}},
        device_id="",
        session_token="",
    )
    assert runtime.browser_rail is True
