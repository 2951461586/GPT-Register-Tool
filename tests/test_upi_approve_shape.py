"""Offline tests for the upi-zero-link-aligned confirm/approve shape.

`UPI_APPROVE_SHAPE=reference` (default) mirrors the reference pure-protocol
rail: PM created separately + ``payment_method: pm_id``,
``payment_method_selection_flow=merchant_specified``, no synthetic
``x-oai-is-client-observation``, and the Checkout-stage Sentinel reused for
approve. `current` keeps the legacy inline-PM path.
"""

from __future__ import annotations

from unittest.mock import patch

from sms_tool.upi_link import sentinel as upi_sentinel
from sms_tool.upi_link.pipeline import _resolve_upi_runtime
from sms_tool.upi_link.stripe import _upi_build_confirm_body

_BASE_RUNTIME = {"upi": {"billing_regions": ["IN"]}}


def _resolve(upi_cfg: dict | None = None, env: dict | None = None):
    runtime = {"upi": {**_BASE_RUNTIME["upi"], **(upi_cfg or {})}}
    with patch.dict("os.environ", env or {}, clear=False):
        return _resolve_upi_runtime(
            access_token="at",
            proxy="",
            checkout_proxy="http://p",
            provider_proxy="",
            approve_proxy="",
            target_country=None,
            checkout_country=None,
            payment_country=None,
            require_zero=None,
            runtime_config=runtime,
            device_id="did",
            session_token="",
        )


def test_reference_shape_is_the_default():
    rc = _resolve(env={"UPI_APPROVE_SHAPE": ""})
    assert rc.approve_shape == "reference"
    assert rc.inline_pm is False
    assert rc.payment_method_selection_flow == "merchant_specified"


def test_current_shape_keeps_inline_pm():
    rc = _resolve({"approve_shape": "current"})
    assert rc.approve_shape == "current"
    assert rc.inline_pm is True
    assert rc.payment_method_selection_flow == "automatic"


def test_confirm_body_with_external_pm_has_no_inline_data():
    body = _upi_build_confirm_body(
        cs_id="cs_live_x",
        stripe_pk="pk_x",
        ctx={},
        processor_entity="openai_ie",
        init_payload={},
        billing={"name": "N", "email": "e", "country": "IN", "line1": "L", "city": "C", "postal_code": "400001"},
        fingerprint={},
        pm_id="pm_x",
        inline_pm=False,
        return_url="https://chatgpt.com/checkout/openai_ie/cs_live_x",
        payment_method_selection_flow="merchant_specified",
    )
    assert body["payment_method"] == "pm_x"
    assert body["client_attribution_metadata[payment_method_selection_flow]"] == "merchant_specified"
    assert not any(key.startswith("payment_method_data[") for key in body)


def test_no_synthetic_observation_without_browser_capture():
    risk = upi_sentinel._UpiRiskContext()
    risk.rotate_observation()
    assert "x-oai-is-client-observation" not in risk.headers()
    risk.browser_observation = True
    risk.observation = "v1.real"
    assert risk.headers()["x-oai-is-client-observation"] == "v1.real"


class _Session:
    def __init__(self):
        self.headers: dict[str, str] = {}


class _Risk:
    sentinel_tokens: dict[str, str] = {}

    def headers(self, *, account_id: str = ""):
        return {}


def test_reference_approve_reuses_checkout_sentinel():
    session = _Session()
    with patch.object(upi_sentinel, "_upi_sentinel_headers", side_effect=AssertionError("must not mint")):
        upi_sentinel._upi_apply_approve_risk(
            session,
            _Risk(),
            "at",
            "did",
            "http://proxy",
            {},
            "",
            checkout_sentinel={"OpenAI-Sentinel-Token": "CO", "OpenAI-Sentinel-SO-Token": "CO_SO"},
            approve_shape="reference",
        )
    assert session.headers["OpenAI-Sentinel-Token"] == "CO"
    assert session.headers["OpenAI-Sentinel-SO-Token"] == "CO_SO"
