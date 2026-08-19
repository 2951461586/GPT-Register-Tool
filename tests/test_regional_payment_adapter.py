import pytest

from sms_tool.checkout_contract import CheckoutRequestContract
from sms_tool.payment_catalog import PAYMENT_METHODS, normalize_payment_method
from sms_tool.payment_link_manager import PAYMENT_ADAPTERS
from sms_tool.regional_payment_adapter import (
    REGIONAL_PAYMENT_PROFILES,
    RegionalPaymentAdapter,
    RegionalPaymentError,
    regional_profile,
    validate_provider_redirect,
)


class FakeTransport:
    def __init__(self, *, redirect="https://midtrans.com/pay/abc", missing_redirect=False):
        self.calls = []
        self.redirect = redirect
        self.missing_redirect = missing_redirect

    def create_checkout(self, request):
        self.calls.append("checkout")
        return {"checkout_session_id": "cs_regional_123456", "publishable_key": "pk_test_regional"}

    def stripe_init(self, request):
        self.calls.append("stripe_init")
        return {"payment_method_types": [request.get("payment_method_type", "gopay"), "gopay", "bizum", "naver_pay"]}

    def create_payment_method(self, request):
        self.calls.append("payment_method")
        return {"payment_method_id": "pm_regional_123456"}

    def confirm_payment(self, request):
        self.calls.append("confirm")
        return {} if self.missing_redirect else {"redirect_url": self.redirect}

    def follow_redirect(self, request):
        self.calls.append("redirect")
        return {"redirect_url": request["redirect_url"], "qr_data": "qris://fixture"}


@pytest.mark.parametrize(
    ("method", "stripe_type", "country", "currency"),
    [("qris", "gopay", "ID", "IDR"), ("bizum", "bizum", "ES", "EUR"), ("naver_pay", "naver_pay", "KR", "KRW")],
)
def test_regional_contract_profiles(method, stripe_type, country, currency):
    profile = regional_profile(method)
    contract = CheckoutRequestContract.for_payment_method(method)
    assert profile.stripe_type == stripe_type
    assert (contract.billing_country, contract.currency, contract.stripe_payment_method) == (country, currency, stripe_type)
    assert PAYMENT_METHODS[method].batch_enabled is True
    assert PAYMENT_METHODS[method].registration_enabled is False


def test_aliases_and_registry_are_exactly_once():
    assert normalize_payment_method("naver-pay") == "naver_pay"
    assert normalize_payment_method("qris_id") == "qris"
    for method in REGIONAL_PAYMENT_PROFILES:
        assert PAYMENT_ADAPTERS.get(method).key == "regional_wallet"
        assert list(PAYMENT_ADAPTERS.methods()).count(method) == 1


def test_capability_probe_has_no_payment_method_or_confirm_side_effects():
    transport = FakeTransport()
    events = []
    result = RegionalPaymentAdapter(regional_profile("qris"), transport).run(
        access_token="at_fixture",
        billing_country="ID",
        probe_only=True,
        progress=events.append,
    )
    assert result["operation"] == "payment_method_capability_probe"
    assert result["side_effects"] is False
    assert transport.calls == ["checkout", "stripe_init"]
    assert [(event["stage"], event["status"]) for event in events] == [
        ("checkout", "running"),
        ("checkout", "completed"),
        ("stripe_init", "running"),
        ("stripe_init", "completed"),
    ]


@pytest.mark.parametrize(
    ("method", "redirect"),
    [("qris", "https://midtrans.com/pay/abc"), ("bizum", "https://bizum.es/pay/abc"), ("naver_pay", "https://pay.naver.com/pay/abc")],
)
def test_full_adapter_uses_independent_provider_host_allowlists(method, redirect):
    transport = FakeTransport(redirect=redirect)
    result = RegionalPaymentAdapter(regional_profile(method), transport).run(
        access_token="at_fixture",
        billing_country=regional_profile(method).country,
    )
    assert result["ok"] is True
    assert result["side_effects"] is True
    assert transport.calls == ["checkout", "stripe_init", "payment_method", "confirm", "redirect"]


def test_provider_host_validation_and_unknown_side_effect_contract():
    with pytest.raises(RegionalPaymentError) as invalid:
        validate_provider_redirect(regional_profile("qris"), "https://evil.example/pay")
    assert invalid.value.error_stage == "redirect"
    assert invalid.value.retryable is False

    transport = FakeTransport(missing_redirect=True)
    with pytest.raises(RegionalPaymentError) as unknown:
        RegionalPaymentAdapter(regional_profile("bizum"), transport).run(
            access_token="at_fixture", billing_country="ES"
        )
    assert unknown.value.status == "unknown"
    assert unknown.value.retryable is False
