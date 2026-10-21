"""The six ``payment_pages`` extractors are offline: refuse them before any Checkout create."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sms_tool.payment import batch as payment_batch
from sms_tool.payment import link_manager as manager
from sms_tool.payment.catalog import (
    OFFLINE_ERROR_CODE,
    PAYMENT_METHODS,
    offline_method_error,
)
from sms_tool.payment.errors import error_code_of, error_stage_of, retryable_of

OFFLINE = ("ideal", "pix", "kakao", "blik", "twint", "momo")
ONLINE = ("paypal", "gopay", "gcash", "grabpay", "upi", "direct_card")

pytestmark = pytest.mark.enforce_offline_methods


@pytest.mark.parametrize("method", OFFLINE)
def test_catalog_marks_payment_pages_extractors_offline_and_hidden(method):
    definition = PAYMENT_METHODS[method]
    assert definition.release_tier == "offline"
    assert definition.batch_enabled is False
    assert definition.registration_enabled is False


@pytest.mark.parametrize("method", ONLINE)
def test_other_methods_are_not_offline(method):
    assert PAYMENT_METHODS[method].release_tier != "offline"
    assert offline_method_error(method) is None


def test_probe_stays_allowed_for_offline_methods():
    assert offline_method_error("momo", probe_only=True) is None


def test_refusal_carries_a_classifiable_non_retryable_code():
    error = offline_method_error("pix")
    assert isinstance(error, ValueError)
    # Read through the accessors, not the attributes: ``payment/errors.py`` is the
    # single place that owns those names and their defaults (the writer side uses
    # ``annotate_error``). Direct ``error.error_code`` was the only reason pyright
    # saw ``ValueError`` with three unknown attributes.
    assert error_code_of(error) == OFFLINE_ERROR_CODE == "payment_method_offline"
    assert error_stage_of(error) == "validation"
    assert retryable_of(error) is False


def test_unknown_method_is_not_reported_as_offline():
    assert offline_method_error("not-a-method") is None


@pytest.mark.parametrize("method", OFFLINE)
def test_generate_refuses_before_any_checkout_or_subprocess(method):
    with (
        patch(
            "sms_tool.pay_link.base.current_config_data",
            return_value={"chatgpt": {}, "protocol_payments": {}},
        ),
        patch("sms_tool.pay_link.core.PAYMENT_ADAPTERS.execute_mapping") as adapter,
        patch("sms_tool.payment.link_manager.subprocess.run") as run,
    ):
        result = manager.generate_payment_link("token", payment_method=method)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error_code"] == OFFLINE_ERROR_CODE
    assert result.get("retryable") in (False, None)
    adapter.assert_not_called()
    run.assert_not_called()


def test_generate_probe_only_is_not_refused_as_offline():
    observation = {"ok": True, "payment_method_types": ["card"], "checkout_kind": "stripe"}
    with (
        patch(
            "sms_tool.pay_link.base.current_config_data",
            return_value={"chatgpt": {}, "protocol_payments": {}},
        ),
        patch("sms_tool.payment.capability.payment_method_capability_probe", return_value=observation),
    ):
        result = manager.generate_payment_link("token", payment_method="momo", probe_only=True)

    assert result.get("error_code") != OFFLINE_ERROR_CODE


@pytest.mark.parametrize("method", OFFLINE)
def test_batch_refuses_offline_method_with_the_offline_code(method):
    with pytest.raises(ValueError, match="payment method is offline") as caught:
        payment_batch.run_payment_batch(["a@example.com"], payment_method=method)
    assert error_code_of(caught.value) == OFFLINE_ERROR_CODE
