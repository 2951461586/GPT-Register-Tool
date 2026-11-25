import pytest

from sms_tool.payment.adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from sms_tool.payment.catalog import PAYMENT_METHODS
from sms_tool.payment.contracts import PaymentRequest, PaymentResult
from sms_tool.payment.link_manager import PAYMENT_ADAPTERS


def test_adapter_registry_routes_methods_and_rejects_duplicates():
    registry = PaymentAdapterRegistry()
    adapter = FunctionPaymentAdapter("fake", ("fake",), lambda **kwargs: {"ok": True})
    registry.register(adapter)
    # A payment adapter registry, not a SQL cursor: the shared SQL-injection
    # heuristic matches any ``.execute(<call>)`` sink.
    result = registry.execute(  # pi-lens-ignore: python-sql-injection
        PaymentRequest.create(payment_method="fake", access_token="at")
    )
    assert isinstance(result, PaymentResult)
    assert result.ok
    with pytest.raises(ValueError):
        registry.register(FunctionPaymentAdapter("other", ("fake",), lambda **kwargs: {}))


def test_default_registry_covers_catalog_exactly_once():
    assert set(PAYMENT_ADAPTERS.methods()) == set(PAYMENT_METHODS)
    for method, definition in PAYMENT_METHODS.items():
        expected_adapter = "team_trial_direct" if method == "team_trial" else "extract_server"
        assert definition.adapter == expected_adapter
        assert definition.script == ""
        assert PAYMENT_ADAPTERS.get(method).key == expected_adapter
