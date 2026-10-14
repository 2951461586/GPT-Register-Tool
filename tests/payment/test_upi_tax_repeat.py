"""Increment 2: re-init + re-tax before confirm (2026-10-01).

The reference measured (2026-09-21) that the ``init -> tax -> init -> tax`` order
before confirm is what makes approve pass reliably.  This drives the real
single-attempt body with the network seams stubbed and counts how many times the
second pass runs under each switch value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sms_tool.upi_link import pipeline as p

_CS = "cs_live_TAX"
_TAX_URL = f"https://api.stripe.com/v1/payment_pages/{_CS}"
_INIT = {
    "stripe_hosted_url": f"https://checkout.stripe.com/c/pay/{_CS}",
    "payment_method_types": ["card", "upi"],
    "currency": "inr",
    "total_summary": {"due": 0, "currency": "inr"},
    "config_id": "cfg_tax",
    "init_checksum": "sum_tax",
}


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = "{}"
        self.headers = {}
        self.url = ""

    def json(self):
        return self._body


class _StripeSession:
    def __init__(self, proxy="", tax_posts=None):
        self.proxy = proxy
        self.headers = {}
        self.tax_posts: list[str] = tax_posts if tax_posts is not None else []

    def post(self, url, json=None, data=None, timeout=None):
        if url == _TAX_URL:
            self.tax_posts.append(url)
            return _Resp(200, dict(_INIT))
        raise AssertionError(url)


class _ChatGPT:
    def __init__(self):
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/backend-api/payments/checkout")
        return _Resp(
            200,
            {"checkout_session_id": _CS, "processor_entity": "openai_ie", "publishable_key": "pk_x"},
        )


def _run_once(*, repeat_tax: bool):
    init_calls: list[str] = []
    tax_posts: list[str] = []
    cfg = {
        "upi": {
            "checkout_country": "IN",
            "payment_country": "IN",
            "require_zero_due": False,
            "repeat_tax_region": repeat_tax,
        }
    }

    def make_stripe(proxy=""):
        return _StripeSession(proxy, tax_posts=tax_posts)

    def fake_init(stripe, cs_id, pk, fingerprint, js_id):
        init_calls.append(cs_id)
        return dict(_INIT)

    with (
        patch.object(p, "_load_json", return_value=cfg),
        patch.object(p, "_upi_new_chatgpt_session", return_value=_ChatGPT()),
        patch.object(p, "_new_session", side_effect=make_stripe),
        patch.object(
            p,
            "_upi_capture_risk_context",
            return_value=SimpleNamespace(headers=lambda **kw: {}, sentinel_tokens={}, stripe_ids={}),
        ),
        patch.object(p, "_upi_sentinel_headers", return_value={}),
        patch.object(p, "_upi_sentinel_ping", return_value=None),
        patch.object(p, "_upi_stripe_init", side_effect=fake_init),
        patch.object(p, "_upi_create_upi_pm", return_value="pm_tax"),
        patch.object(p, "_upi_build_confirm_body", return_value={}),
        patch.object(p, "_upi_post_with_degrade", return_value=(_Resp(400, {}), None)),
        patch.object(p, "_upi_hosted_fallback_result", return_value={"ok": False, "error_code": "stop_after_tax"}),
        patch.object(p, "_upi_assert_egress_contract", return_value=None),
        patch.object(p, "_upi_dump_http", return_value=None),
    ):
        result = p._generate_upi_qr_link_once("at", checkout_proxy="http://u:p@exit:1")
    return result, init_calls, tax_posts


def test_repeat_tax_region_defaults_on_and_honours_config_and_env(monkeypatch):
    monkeypatch.delenv("UPI_REPEAT_TAX_REGION", raising=False)
    assert p._upi_repeat_tax_region({}) is True
    assert p._upi_repeat_tax_region({"repeat_tax_region": False}) is False
    monkeypatch.setenv("UPI_REPEAT_TAX_REGION", "0")
    assert p._upi_repeat_tax_region({}) is False


def test_second_init_and_tax_run_before_confirm_when_enabled():
    result, init_calls, tax_posts = _run_once(repeat_tax=True)

    assert result["error_code"] == "stop_after_tax"
    assert init_calls == [_CS, _CS]
    assert tax_posts == [_TAX_URL, _TAX_URL]


def test_second_pass_is_skipped_when_disabled():
    result, init_calls, tax_posts = _run_once(repeat_tax=False)

    assert result["error_code"] == "stop_after_tax"
    assert init_calls == [_CS]
    assert tax_posts == [_TAX_URL]
