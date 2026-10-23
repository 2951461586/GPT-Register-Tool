"""The pay_link adapters may not invent or drop keyword arguments.

Why this exists
---------------
``pay_link.registry.paypal_runner`` used to forward a **literal 14-name
whitelist** to ``generate_pp_link``. A hand-maintained whitelist drifts in two
directions, and both are silent:

* **grow past the callee** -> ``TypeError: generate_pp_link() got an unexpected
  keyword argument`` at runtime, on the path a user actually paid for. Measured
  2026-10-23: ``max_checkout_retries`` and ``max_stage_retries`` were on the
  whitelist and were *not* parameters of ``generate_pp_link``. Nothing caught it
  because no test drove the pay_link seam with those options -- the callee reads
  them from ``paypal.*`` config, so the adapter was the only caller that could
  pass them as options.
* **fall behind the callee** -> a new option is accepted by the entrypoint while
  the adapter keeps dropping it. This already happened on the UPI side, where
  ``wait_paid`` / ``paid_timeout`` / ``require_server_upi_mandate`` were dropped
  (``test_upi_link_entrypoint_unique.py`` pins that fix).

The whitelist is gone: the runner now asks the entrypoint's signature
(``pay_link.base._forwardable_kwargs``), so neither direction can drift. These
tests pin the mechanism on a stand-in callee (a new parameter is forwarded, an
unknown one is dropped, ``None`` is dropped, explicitly passed names are not
repeated) and the real seam end to end.

``StageProxies`` collapses the seven proxy strings into one value at the
entrypoint boundary; the per-stage keywords stay supported, and both spellings
are covered here.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

#: Keyword arguments the runner consumes itself instead of forwarding.
_PAYPAL_HANDLED = frozenset(
    {
        "access_token",
        "proxy",
        "auth_context",
        "payment_method",
        "runtime_config",
        "paypal_generation_type",
    }
)

#: Values for every forwardable ``generate_pp_link`` parameter. Kept explicit
#: (rather than generated) so a new parameter forces a decision here.
_PAYPAL_SAMPLE = {
    "checkout_proxy": "http://checkout:1",
    "provider_proxy": "http://provider:1",
    "stripe_init_proxy": "http://stripe-init:1",
    "payment_method_proxy": "http://payment-method:1",
    "confirm_proxy": "http://confirm:1",
    "approve_proxy": "http://approve:1",
    "promotion_proxy": "http://promotion:1",
    "target_country": "US",
    "checkout_country": "US",
    "require_zero": True,
    "require_ba_token": False,
    "stage_proxy_countries": {"checkout": "US", "approve": "US"},
    "max_checkout_retries": 2,
    "max_stage_retries": 3,
    "stage_proxies": {"checkout": "http://checkout:1", "approve": "http://approve:1"},
}


def _paypal_entrypoint():
    from sms_tool.paypal_link.gen_link import generate_pp_link

    return generate_pp_link


def _forwardable_parameters(function) -> list[str]:
    return [
        name
        for name, parameter in inspect.signature(function).parameters.items()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]


def _drive_paypal_adapter(options: dict):
    """Execute the paypal runner once; return what the entrypoint received."""
    from sms_tool.payment.contracts import PaymentRequest
    from sms_tool.pay_link.registry import build_default_payment_registry

    registry = build_default_payment_registry()
    request = PaymentRequest.create(
        payment_method="paypal",
        access_token="at-test",
        options=options,
    )
    # ``autospec`` matters: forwarding is signature-driven, so a bare MagicMock
    # (``(*args, **kwargs)``) would accept every key and turn the assertions
    # below into tautologies.
    from sms_tool.paypal_link.gen_link import generate_pp_link as real_entrypoint

    with (
        patch(
            "sms_tool.gen_pp_link.generate_pp_link",
            autospec=real_entrypoint,
            return_value={"ok": True},
        ) as entrypoint,
        patch("sms_tool.pay_link.registry._assert_function_adapter_egress", return_value=None),
    ):
        registry.execute_mapping(request)
    return entrypoint.call_args.kwargs


def test_forwarding_is_signature_driven():
    """The mechanism: a callee parameter is forwarded without editing any list."""
    from sms_tool.pay_link.base import _forwardable_kwargs

    def callee(access_token: str, *, newly_added_option: int = 0, other: int = 0):
        return None

    values = {
        "access_token": "at",
        "newly_added_option": 7,
        "other": None,
        "not_a_parameter": 3,
    }
    forwarded = _forwardable_kwargs(callee, values, skip={"access_token"})
    assert forwarded == {"newly_added_option": 7}, forwarded


def test_a_renamed_parameter_is_dropped_instead_of_raising():
    """The old whitelist raised TypeError here; the signature filter drops it."""

    def callee(access_token: str, *, renamed_option: int = 0):
        return None

    from sms_tool.pay_link.base import _forwardable_kwargs

    forwarded = _forwardable_kwargs(callee, {"access_token": "at", "old_option": 5}, skip={"access_token"})
    assert forwarded == {}, forwarded
    callee(**{"access_token": "at", **_forwardable_kwargs(callee, {"old_option": 5, "renamed_option": 5})})


def test_the_sample_covers_every_forwardable_parameter():
    """Guard the guard: a new parameter must be added to the sample, not ignored."""
    parameters = set(_forwardable_parameters(_paypal_entrypoint())) - _PAYPAL_HANDLED
    assert parameters == set(_PAYPAL_SAMPLE), (
        "generate_pp_link's forwardable parameters drifted from the sample used by "
        "the pay_link parity test; add or remove the entry here so the two-way "
        "check keeps meaning something."
    )


def test_no_forwarded_keyword_is_unknown_to_the_entrypoint():
    """The TypeError guard: every forwarded option is a real parameter."""
    entrypoint = _paypal_entrypoint()
    parameters = set(inspect.signature(entrypoint).parameters)
    forwarded = _drive_paypal_adapter(dict(_PAYPAL_SAMPLE))

    unexpected = sorted(set(forwarded) - parameters)
    assert not unexpected, (
        f"paypal_runner forwards {unexpected} to generate_pp_link, which does not "
        "accept them -- the adapter path raises TypeError as soon as a caller sets "
        "one of those options. Either add the parameter to generate_pp_link (and "
        "document the override) or delete it from the whitelist in "
        "sms_tool/pay_link/registry.py."
    )


def test_every_entrypoint_option_reaches_the_entrypoint():
    """The silent-drop guard: nothing the entrypoint accepts is filtered out."""
    forwarded = _drive_paypal_adapter(dict(_PAYPAL_SAMPLE))
    dropped = sorted(set(_PAYPAL_SAMPLE) - set(forwarded))
    assert not dropped, (
        f"paypal_runner silently drops {dropped}: the option is accepted by "
        "generate_pp_link but missing from the whitelist in "
        "sms_tool/pay_link/registry.py. A caller who sets it sees the configured "
        "default instead, with no error."
    )
    assert forwarded["max_stage_retries"] == 3
    assert forwarded["max_checkout_retries"] == 2


def test_unknown_options_are_dropped_not_forwarded():
    """Options owned by other lanes must not leak into the PayPal entrypoint."""
    forwarded = _drive_paypal_adapter(
        {
            "approve_country": "JP",
            "native_upi_qr_path": "/tmp/x.png",
            "totally_unknown_option": 1,
        }
    )
    assert not ({"approve_country", "native_upi_qr_path", "totally_unknown_option"} & set(forwarded))


def test_upi_adapter_options_are_all_parameters_of_the_entrypoint():
    """Same two-way contract for the ``native_upi`` runner."""
    from sms_tool.payment.contracts import PaymentRequest
    from sms_tool.pay_link.registry import build_default_payment_registry
    from sms_tool.upi_link.pipeline import generate_upi_qr_link

    parameters = set(inspect.signature(generate_upi_qr_link).parameters)
    options = {
        "checkout_proxy": "http://checkout:1",
        "provider_proxy": "http://provider:1",
        "approve_proxy": "http://approve:1",
        "target_country": "IN",
        "checkout_country": "IN",
        "payment_country": "IN",
        "require_zero": True,
        "qr_path": "runtime/tmp/qr.png",
        "wait_paid": False,
        "paid_timeout": 30.0,
        "require_server_upi_mandate": False,
    }
    registry = build_default_payment_registry()
    request = PaymentRequest.create(payment_method="upi", access_token="at-test", options=options)
    with patch("sms_tool.upi_link.generate_upi_qr_link", return_value={"ok": True}) as entrypoint:
        registry.execute_mapping(request)

    forwarded = set(entrypoint.call_args.kwargs)
    assert not forwarded - parameters, (
        f"native_upi forwards {sorted(forwarded - parameters)} which "
        "generate_upi_qr_link does not accept (TypeError on the adapter path)."
    )
    dropped = sorted(set(options) - forwarded)
    assert not dropped, f"native_upi drops {dropped} before the entrypoint sees them"


@pytest.mark.parametrize(
    "option",
    ["checkout_proxy", "provider_proxy", "stripe_init_proxy", "payment_method_proxy", "confirm_proxy"],
)
def test_stage_proxy_options_reach_the_extractor_constructor(option):
    """End of the chain: the option is not dropped after ``generate_pp_link``.

    ``generate_pp_link`` builds ``PPLinkExtractor`` with the resolved proxies.
    A whitelisted option that stops there would still be a silent drop, so pin
    the wiring for the six proxy strings the adapter advertises.
    """
    from sms_tool.paypal_link import gen_link

    captured: dict = {}

    class FakeExtractor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def extract(self):
            return {"ok": False, "error": "stop-after-construction", "url": "", "ba_token": ""}

    cfg = {
        "paypal": {
            "target_country": "US",
            "checkout_country": "US",
            "require_zero_due": False,
            "require_ba_token": False,
        },
        "proxy": {},
    }
    with patch.object(gen_link._plm, "PPLinkExtractor", FakeExtractor):
        gen_link.generate_pp_link(
            "at-test",
            runtime_config=cfg,
            **_PAYPAL_SAMPLE,
        )

    assert captured, "PPLinkExtractor was never constructed -- the test drove nothing"
    assert captured[option] == _PAYPAL_SAMPLE[option], (
        f"{option} did not reach PPLinkExtractor; it was dropped inside "
        "generate_pp_link after the adapter forwarded it."
    )
