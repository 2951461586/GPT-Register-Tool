import pytest

from sms_tool.payment_adapters import FunctionPaymentAdapter, PaymentAdapterRegistry
from sms_tool.payment_catalog import PAYMENT_METHODS
from sms_tool.payment_contracts import PaymentRequest, PaymentResult
from sms_tool.payment_link_manager import PAYMENT_ADAPTERS, _reference_root


def test_adapter_registry_routes_methods_and_rejects_duplicates():
    registry = PaymentAdapterRegistry()
    adapter = FunctionPaymentAdapter("fake", ("fake",), lambda **kwargs: {"ok": True})
    registry.register(adapter)
    result = registry.execute(PaymentRequest.create(payment_method="fake", access_token="at"))
    assert isinstance(result, PaymentResult)
    assert result.ok
    with pytest.raises(ValueError):
        registry.register(FunctionPaymentAdapter("other", ("fake",), lambda **kwargs: {}))


def test_default_registry_covers_catalog_exactly_once():
    assert set(PAYMENT_ADAPTERS.methods()) == set(PAYMENT_METHODS)
    for method, definition in PAYMENT_METHODS.items():
        assert PAYMENT_ADAPTERS.get(method).key == definition.adapter


@pytest.mark.parametrize("method", sorted(PAYMENT_METHODS))
def test_catalog_script_paths_exist(method):
    definition = PAYMENT_METHODS[method]
    if definition.script:
        assert (_reference_root() / definition.script).is_file()
