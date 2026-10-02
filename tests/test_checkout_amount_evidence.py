"""Authoritative amount + discount evidence for capability probes (2026-10-01 scan P1-2).

The reference ``promo_detector`` records that a payload carrying both
``total.total`` and ``total.taxInclusive`` (a tax component) must not be read as
"amount observations disagree, so the amount is unknown".  These tests pin the
authoritative-path ordering and the new discount/observation evidence.
"""

from __future__ import annotations

from sms_tool.checkout_contract import (
    CheckoutRequestContract,
    StripeCapabilityEvidence,
    amount_observations,
    discount_breakdown,
    payable_amount,
)
from sms_tool.payment_capability import build_capability_probe_result


def test_payable_amount_prefers_due_over_tax_component():
    payload = {
        "total_summary": {"due": 0},
        "total": {"subtotal": 2900, "discount": 2900, "total": 0, "taxInclusive": 0},
    }
    assert payable_amount(payload) == ("total_summary.due", 0)


def test_payable_amount_unwraps_nested_state():
    payload = {"checkout_session": {"invoice": {"amount_due": 1234}}}
    assert payable_amount(payload) == ("invoice.amount_due", 1234)


def test_payable_amount_returns_none_without_a_readable_amount():
    assert payable_amount({"total": {"taxInclusive": 0}}) is None


def test_discount_breakdown_reads_the_total_block():
    payload = {"total": {"subtotal": 2900, "discount": 2900, "total": 0}}
    assert discount_breakdown(payload) == (2900, 2900, 0)


def test_amount_observations_are_deduplicated_and_include_the_tax_component():
    payload = {
        "total_summary": {"due": 0},
        "total": {"total": 0, "taxInclusive": 0},
    }
    observations = amount_observations(payload)
    assert observations[0] == ("total_summary.due", 0)
    assert len(observations) == len(set(observations))
    assert ("total.taxInclusive", 0) in observations


def test_zero_due_with_a_real_discount_is_labelled_discounted():
    evidence = StripeCapabilityEvidence.from_payload(
        {
            "total_summary": {"due": 0},
            "total": {"subtotal": 2900, "discount": 2900, "total": 0},
            "currency": "usd",
            "payment_method_types": ["paypal"],
        }
    )
    assert evidence.amount_minor == 0
    assert evidence.offer_state == "discounted_zero_due"
    assert evidence.discount_breakdown == (2900, 2900, 0)


def test_plain_zero_due_stays_zero_due():
    evidence = StripeCapabilityEvidence.from_payload(
        {
            "total_summary": {"due": 0},
            "currency": "usd",
            "payment_method_types": ["paypal"],
        }
    )
    assert evidence.offer_state == "zero_due"
    assert evidence.discount_breakdown == (None, None, None)


def test_capability_result_carries_the_amount_evidence():
    contract = CheckoutRequestContract.for_payment_method("paypal")
    evidence = StripeCapabilityEvidence.from_payload(
        {
            "total_summary": {"due": 0},
            "total": {"subtotal": 2900, "discount": 2900, "total": 0},
            "currency": "usd",
            "payment_method_types": ["paypal"],
        }
    )

    result = build_capability_probe_result(contract, evidence, checkout_session_present=True, require_zero=True)

    assert result["eligible"] is True
    assert result["offer_state"] == "discounted_zero_due"
    assert result["discount_breakdown"] == [2900, 2900, 0]
    assert all(isinstance(item, list) and len(item) == 2 for item in result["amount_observations"])
