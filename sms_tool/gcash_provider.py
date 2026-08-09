"""Typed GCash custom-payment-method flow.

GCash is exposed by ChatGPT Checkout as a custom payment method, not as a
normal Stripe PaymentMethod. This module owns that protocol and keeps the
wire implementation behind ``GCashProviderTransport``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from .checkout_contract import CheckoutRequestContract, CheckoutSessionContract, StripeCapabilityEvidence
from .sanitizer import sanitize_text


DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID = "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ"
_ADYEN_REDIRECT_HOSTS = frozenset({
    "checkoutshopper-live.adyen.com",
    "checkoutshopper-test.adyen.com",
})
_UNKNOWN_AFTER_CONFIRM = frozenset({"confirm", "start"})


@dataclass(frozen=True)
class GCashTransportRequest:
    stage: str
    contract: CheckoutRequestContract
    checkout_session_id: str = ""
    processor_entity: str = ""
    access_token: str = field(default="", repr=False)
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    auth_context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    transport_context: Mapping[str, Any] = field(default_factory=dict, repr=False)


class GCashProviderTransport(Protocol):
    def create_checkout(self, request: GCashTransportRequest) -> Mapping[str, Any]: ...
    def update_checkout(self, request: GCashTransportRequest) -> Mapping[str, Any]: ...
    def update_taxes(self, request: GCashTransportRequest) -> Mapping[str, Any]: ...
    def resolve_checkout(self, request: GCashTransportRequest) -> Mapping[str, Any]: ...
    def probe_custom_payment(self, request: GCashTransportRequest) -> Mapping[str, Any]: ...
    def confirm_custom_payment(self, request: GCashTransportRequest) -> Mapping[str, Any]: ...
    def start_custom_payment(self, request: GCashTransportRequest) -> Mapping[str, Any]: ...


class GCashProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "gcash_provider_failed",
        error_stage: str = "gcash",
        retryable: bool = False,
        status: str = "failed",
    ) -> None:
        self.error_code = error_code
        self.error_stage = error_stage
        self.retryable = bool(retryable)
        self.status = status if status in {"failed", "cancelled", "timed_out", "unknown"} else "unknown"
        super().__init__(sanitize_text(message))


@dataclass(frozen=True)
class GCashCapability:
    custom_payment_method_type_id: str
    method_available: bool | None
    amount_minor: int | None
    currency: str

    @classmethod
    def from_payloads(
        cls,
        *payloads: Mapping[str, Any],
        configured_type_id: str = "",
    ) -> "GCashCapability":
        type_id = ""
        available: bool | None = None
        amount: int | None = None
        currency = "PHP"
        saw_method_evidence = False
        for payload in payloads:
            if not isinstance(payload, Mapping):
                continue
            for item in _walk_mappings(payload):
                label = str(item.get("display_name") or item.get("name") or item.get("type") or "").strip().lower()
                candidate = str(
                    item.get("custom_payment_method_type_id")
                    or item.get("payment_method_type_id")
                    or item.get("type")
                    or item.get("id")
                    or ""
                ).strip()
                if label == "gcash" or (candidate.startswith("cpmt_") and "gcash" in str(item).lower()):
                    available = True
                    if candidate.startswith("cpmt_"):
                        type_id = candidate
            methods = _method_tokens(payload)
            if _has_custom_method_evidence(payload):
                saw_method_evidence = True
            if "gcash" in methods:
                available = True
            evidence = StripeCapabilityEvidence.from_payload(dict(payload), fallback_currency="PHP")
            if evidence.amount_minor is not None:
                amount = evidence.amount_minor
            if evidence.currency:
                currency = evidence.currency
        if available is None and saw_method_evidence:
            available = False
        return cls(type_id or str(configured_type_id or "").strip(), available, amount, currency)


def run_gcash_provider(
    access_token: str,
    transport: GCashProviderTransport,
    *,
    probe_only: bool = False,
    auth_context: Mapping[str, Any] | None = None,
    transport_context: Mapping[str, Any] | None = None,
    custom_payment_method_type_id: str = DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID,
    require_zero: bool = True,
) -> dict[str, Any]:
    stage = "validation"
    capability: GCashCapability | None = None
    warnings: list[dict[str, str]] = []
    try:
        token = str(access_token or "").strip()
        if not token:
            raise GCashProviderError(
                "access_token is required", error_code="missing_access_token", error_stage=stage
            )
        contract = CheckoutRequestContract.for_payment_method("gcash")
        context = dict(auth_context or {})
        context.setdefault("device_id", str(uuid.uuid4()))
        wire_context = dict(transport_context or {})

        stage = "checkout"
        checkout_request = dict(contract.checkout_payload())
        checkout_request["check_card_proxy"] = True
        checkout_payload = transport.create_checkout(_request(
            stage, contract, token, checkout_request, context, wire_context
        ))
        session = CheckoutSessionContract.from_payload(
            dict(checkout_payload), billing_country=contract.billing_country
        )

        stage = "update"
        update_payload = {
            "checkout_session_id": session.checkout_session_id,
            "processor_entity": session.processor_entity,
            "plan_name": contract.plan_name,
            "price_interval": "month",
            "seat_quantity": 1,
            "promo_campaign": {
                "promo_campaign_id": contract.promo_campaign_id,
                "is_coupon_from_query_param": False,
            },
        }
        update_response: Mapping[str, Any] = {}
        try:
            update_response = transport.update_checkout(_request(
                stage, contract, token, update_payload, context, wire_context, session
            ))
        except Exception as exc:
            warnings.append({"stage": stage, "error": sanitize_text(exc)})

        stage = "taxes"
        try:
            transport.update_taxes(_request(
                stage, contract, token, _tax_payload(contract, session), context, wire_context, session
            ))
        except Exception as exc:
            warnings.append({"stage": stage, "error": sanitize_text(exc)})

        stage = "resolve"
        resolved = transport.resolve_checkout(_request(
            stage, contract, token, {}, context, wire_context, session
        ))
        stage = "custom_capability"
        custom_capability: Mapping[str, Any] = {}
        try:
            custom_capability = transport.probe_custom_payment(_request(
                stage,
                contract,
                token,
                {
                    "checkout": checkout_payload,
                    "custom_payment_method_type_id": custom_payment_method_type_id,
                },
                context,
                wire_context,
                session,
            ))
        except Exception as exc:
            warnings.append({"stage": stage, "error": sanitize_text(exc)})
        custom_probe = GCashCapability.from_payloads(
            custom_capability,
            configured_type_id=custom_payment_method_type_id,
        )
        requested_zero_evidence: Mapping[str, Any] = {}
        if custom_probe.method_available is True and custom_probe.amount_minor is None:
            requested_zero_evidence = {"amount_due": 0, "currency": contract.currency}
        capability = GCashCapability.from_payloads(
            checkout_payload,
            update_response,
            resolved,
            custom_capability,
            requested_zero_evidence,
            configured_type_id=custom_payment_method_type_id,
        )
        probe = _capability_result(capability, require_zero=require_zero)
        if probe_only:
            return {
                **probe,
                "payment_method": "gcash",
                "operation": "payment_method_capability_probe",
                "warnings": warnings,
                "error_stage": "" if probe["conclusive"] else "capability_classification",
                "error_code": "" if probe["conclusive"] else str(probe["decision"]),
                "error": "" if probe["conclusive"] else str(probe["decision"]),
                "retryable": not probe["conclusive"],
            }
        if capability.method_available is False:
            raise GCashProviderError(
                "GCash is not offered by this checkout",
                error_code="payment_method_unavailable",
                error_stage="resolve",
            )
        if capability.method_available is None:
            raise GCashProviderError(
                "GCash capability evidence is inconclusive",
                error_code="gcash_capability_unknown",
                error_stage="resolve",
                status="unknown",
            )
        if require_zero and capability.amount_minor not in (0,):
            code = "checkout_amount_unknown" if capability.amount_minor is None else "nonzero_offer"
            raise GCashProviderError(
                code,
                error_code=code,
                error_stage="resolve",
                status="unknown" if capability.amount_minor is None else "failed",
            )
        if not capability.custom_payment_method_type_id.startswith("cpmt_"):
            raise GCashProviderError(
                "GCash custom payment method id is missing",
                error_code="gcash_custom_method_id_missing",
                error_stage="resolve",
                status="unknown",
            )

        stage = "confirm"
        transport.confirm_custom_payment(_request(
            stage,
            contract,
            token,
            {
                "checkout_session_id": session.checkout_session_id,
                "type": "custom_payment_method",
                "selected_payment_method_type": capability.custom_payment_method_type_id,
            },
            context,
            wire_context,
            session,
        ))

        stage = "start"
        started = transport.start_custom_payment(_request(
            stage,
            contract,
            token,
            {
                "checkout_session_id": session.checkout_session_id,
                "custom_payment_method_type_id": capability.custom_payment_method_type_id,
            },
            context,
            wire_context,
            session,
        ))
        redirect_url = _next_action_url(started)
        _validate_adyen_url(redirect_url)
        return {
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "payment_method": "gcash",
            "link_type": "gcash_adyen_redirect",
            "url": redirect_url,
            "provider_redirect_url": redirect_url,
            "capability": probe,
            "warnings": warnings,
            "retryable": False,
            "error_stage": "",
        }
    except asyncio.CancelledError:
        return _failure(GCashProviderError(
            "GCash flow was cancelled", error_code="gcash_cancelled", error_stage=stage, status="cancelled"
        ), capability)
    except concurrent.futures.CancelledError:
        return _failure(GCashProviderError(
            "GCash flow was cancelled", error_code="gcash_cancelled", error_stage=stage, status="cancelled"
        ), capability)
    except Exception as exc:
        return _failure(_structured_error(exc, stage), capability)


def _request(
    stage: str,
    contract: CheckoutRequestContract,
    token: str,
    payload: Mapping[str, Any],
    auth_context: Mapping[str, Any],
    transport_context: Mapping[str, Any],
    session: CheckoutSessionContract | None = None,
) -> GCashTransportRequest:
    return GCashTransportRequest(
        stage=stage,
        contract=contract,
        checkout_session_id=session.checkout_session_id if session else "",
        processor_entity=session.processor_entity if session else "",
        access_token=token,
        payload=payload,
        auth_context=auth_context,
        transport_context=transport_context,
    )


def _capability_result(capability: GCashCapability, *, require_zero: bool) -> dict[str, Any]:
    eligible: bool | None = capability.method_available
    decision = "payment_method_available" if eligible else "payment_method_unavailable"
    if eligible and require_zero and capability.amount_minor is None:
        eligible, decision = None, "checkout_amount_unknown"
    elif eligible and require_zero and capability.amount_minor != 0:
        eligible, decision = False, "nonzero_offer"
    classification = "eligible" if eligible is True else "ineligible" if eligible is False else "unknown"
    return {
        "ok": classification != "unknown",
        "status": "completed" if classification != "unknown" else "unknown",
        "classification": classification,
        "decision": decision,
        "eligible": eligible,
        "method_available": capability.method_available,
        "conclusive": classification != "unknown",
        "amount": capability.amount_minor,
        "currency": capability.currency,
        "custom_payment_method_type_id": capability.custom_payment_method_type_id,
        "offer_state": (
            "zero_due" if capability.amount_minor == 0
            else "nonzero_due" if capability.amount_minor is not None
            else "unknown_amount"
        ),
        "custom_payment_method_present": bool(capability.custom_payment_method_type_id),
    }


def _tax_payload(contract: CheckoutRequestContract, session: CheckoutSessionContract) -> dict[str, Any]:
    return {
        "checkout_session_id": session.checkout_session_id,
        "checkout_email": "buyer@example.com",
        "billing_country": contract.billing_country,
        "billing_name": "Maria Santos",
        "currency": contract.currency,
        "tax_id": None,
        "processor_entity": session.processor_entity,
        "billing_address": {
            "country": contract.billing_country,
            "line1": "6750 Ayala Avenue",
            "line2": "",
            "city": "Makati",
            "state": "Metro Manila",
            "postal_code": "1226",
        },
    }


def _next_action_url(payload: Mapping[str, Any]) -> str:
    action = payload.get("next_action") if isinstance(payload, Mapping) else None
    return str(action.get("url") or "").strip() if isinstance(action, Mapping) else ""


def _validate_adyen_url(url: str) -> None:
    parsed = urlsplit(str(url or "").strip())
    host = str(parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or host not in _ADYEN_REDIRECT_HOSTS:
        raise GCashProviderError(
            "GCash redirect host is not allowed",
            error_code="gcash_redirect_host_not_allowed",
            error_stage="start",
            status="unknown",
        )


def _method_tokens(payload: Mapping[str, Any]) -> set[str]:
    output: set[str] = set()
    for item in _walk_values(payload):
        if isinstance(item, list):
            for value in item:
                if isinstance(value, str):
                    output.add(value.strip().lower().replace("-", "_"))
                elif isinstance(value, Mapping):
                    token = str(value.get("type") or value.get("display_name") or value.get("name") or "")
                    if token:
                        output.add(token.strip().lower().replace("-", "_"))
    return output


def _walk_values(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {
                "payment_method_types", "ordered_payment_method_types",
                "custom_payment_methods", "custom_payment_method_data",
            }:
                yield item
            if isinstance(item, (Mapping, list)):
                yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (Mapping, list)):
                yield from _walk_values(item)


def _has_custom_method_evidence(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in {"custom_payment_methods", "custom_payment_method_data"}:
                return True
            if isinstance(item, (Mapping, list)) and _has_custom_method_evidence(item):
                return True
    elif isinstance(value, list):
        return any(
            _has_custom_method_evidence(item)
            for item in value
            if isinstance(item, (Mapping, list))
        )
    return False


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            if isinstance(item, (Mapping, list)):
                yield from _walk_mappings(item)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (Mapping, list)):
                yield from _walk_mappings(item)


def _structured_error(exc: BaseException, stage: str) -> GCashProviderError:
    if isinstance(exc, GCashProviderError):
        return exc
    if isinstance(exc, TimeoutError):
        return GCashProviderError(
            f"{stage} timed out", error_code="gcash_timed_out", error_stage=stage,
            retryable=stage not in _UNKNOWN_AFTER_CONFIRM,
            status="unknown" if stage in _UNKNOWN_AFTER_CONFIRM else "timed_out",
        )
    return GCashProviderError(
        f"{stage} failed: {type(exc).__name__}",
        error_code="gcash_result_unknown" if stage in _UNKNOWN_AFTER_CONFIRM else "gcash_transport_failed",
        error_stage=stage,
        retryable=stage not in _UNKNOWN_AFTER_CONFIRM,
        status="unknown" if stage in _UNKNOWN_AFTER_CONFIRM else "failed",
    )


def _failure(error: GCashProviderError, capability: GCashCapability | None) -> dict[str, Any]:
    return {
        "ok": False,
        "status": error.status,
        "operation": "extract_link",
        "payment_method": "gcash",
        "url": "",
        "error": str(error),
        "error_code": error.error_code,
        "error_stage": error.error_stage,
        "retryable": error.retryable,
        "requires_reconciliation": error.status == "unknown" and error.error_stage in _UNKNOWN_AFTER_CONFIRM,
        "capability": _capability_result(capability, require_zero=False) if capability else None,
    }


__all__ = [
    "DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID",
    "GCashCapability",
    "GCashProviderError",
    "GCashProviderTransport",
    "GCashTransportRequest",
    "run_gcash_provider",
]
