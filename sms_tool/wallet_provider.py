"""Shared orchestration core for redirect-based wallet payment methods.

The module deliberately owns no HTTP sessions.  A caller supplies a
``WalletProviderTransport`` which performs the wire calls described by the
typed requests below.  This keeps the protocol contract testable while letting
the desktop application retain ownership of proxies, cookies and TLS clients.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from .checkout_contract import (
    CheckoutContractError,
    CheckoutRequestContract,
    CheckoutSessionContract,
    PAYMENT_METHOD_PROFILES,
    StripeCapabilityEvidence,
)
from .payment_capability import build_capability_probe_result
from .sanitizer import sanitize_text as _canonical_sanitize_text


_RUNTIME_VERSION = "6f8494a281"
_SIDE_EFFECT_STAGES = frozenset({"confirm", "approve", "poll", "follow_redirect"})
_TERMINAL_FAILURE_STATES = frozenset({"declined", "denied", "failed", "rejected"})
_TERMINAL_CANCEL_STATES = frozenset({"canceled", "cancelled"})
_SESSION_ID_RE = re.compile(r"\b(?:cs|oaics|pm|pi|seti)_(?:live_|test_)?[A-Za-z0-9_]{8,}\b")

@dataclass(frozen=True)
class WalletMethodSpec:
    """Wallet-specific behavior layered on the shared Checkout profile."""

    key: str
    label: str
    redirect_hosts: tuple[str, ...]

    @property
    def country(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].country

    @property
    def currency(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].currency

    @property
    def locale(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].payment_locale

    @property
    def stripe_type(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.key].stripe_type


WALLET_METHODS: dict[str, WalletMethodSpec] = {
    "gopay": WalletMethodSpec("gopay", "GoPay", ("gopay.co.id", "gojek.com", "midtrans.com")),
    "grabpay": WalletMethodSpec("grabpay", "GrabPay", ("grab.com", "grabpay.com")),
}


@dataclass(frozen=True)
class WalletFlowIdentifiers:
    stripe_js_id: str
    elements_session_id: str
    elements_session_config_id: str
    runtime_version: str = _RUNTIME_VERSION

    @classmethod
    def create(cls, uuid_factory: Callable[[], Any] = uuid.uuid4) -> "WalletFlowIdentifiers":
        stripe_js_id = str(uuid_factory())
        session_suffix = str(uuid_factory()).replace("-", "")[:11]
        config_id = str(uuid_factory())
        return cls(stripe_js_id, f"elements_session_{session_suffix}", config_id)


@dataclass(frozen=True)
class WalletTransportRequest:
    """A redaction-safe request envelope passed to the injected transport."""

    stage: str
    method: str
    contract: CheckoutRequestContract
    flow_id: str
    attempt: int = 1
    checkout_session_id: str = ""
    processor_entity: str = ""
    access_token: str = field(default="", repr=False)
    publishable_key: str = field(default="", repr=False)
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    auth_context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    transport_context: Mapping[str, Any] = field(default_factory=dict, repr=False)
    redirect_url: str = field(default="", repr=False)


class WalletProviderTransport(Protocol):
    """Wire boundary implemented by the application HTTP/proxy layer."""

    def create_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def update_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def stripe_init(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def create_payment_method(self, request: WalletTransportRequest) -> Mapping[str, Any] | str: ...

    def confirm_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def approve_checkout(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def poll_payment(self, request: WalletTransportRequest) -> Mapping[str, Any]: ...

    def follow_redirect(self, request: WalletTransportRequest) -> Mapping[str, Any] | str: ...


class WalletProviderError(RuntimeError):
    """Structured adapter failure safe to expose to callers and persistence."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "wallet_provider_failed",
        error_stage: str = "wallet_provider",
        retryable: bool = False,
        status: str = "failed",
    ) -> None:
        self.error_code = str(error_code or "wallet_provider_failed")
        self.error_stage = str(error_stage or "wallet_provider")
        self.retryable = bool(retryable)
        self.status = status if status in {"failed", "cancelled", "timed_out", "unknown"} else "unknown"
        super().__init__(redact_sensitive_text(message))


class WalletCancelledError(WalletProviderError):
    def __init__(self, message: str = "wallet flow was cancelled", *, error_stage: str = "wallet_provider") -> None:
        super().__init__(
            message,
            error_code="wallet_cancelled",
            error_stage=error_stage,
            retryable=False,
            status="cancelled",
        )


class WalletTimedOutError(WalletProviderError):
    def __init__(self, message: str = "wallet flow timed out", *, error_stage: str = "wallet_provider") -> None:
        super().__init__(
            message,
            error_code="wallet_timed_out",
            error_stage=error_stage,
            retryable=True,
            status="timed_out",
        )


class WalletUnknownResultError(WalletProviderError):
    def __init__(self, message: str, *, error_stage: str) -> None:
        super().__init__(
            message,
            error_code="wallet_result_unknown",
            error_stage=error_stage,
            retryable=False,
            status="unknown",
        )


def wallet_method_spec(payment_method: Any) -> WalletMethodSpec:
    key = str(payment_method or "").strip().lower().replace("-", "").replace("_", "")
    aliases = {"gopay": "gopay", "grabpay": "grabpay"}
    normalized = aliases.get(key, "")
    if not normalized:
        raise WalletProviderError(
            f"unsupported wallet payment method: {payment_method}",
            error_code="wallet_method_unsupported",
            error_stage="validation",
        )
    return WALLET_METHODS[normalized]


def redact_sensitive_text(value: Any) -> str:
    """Redact bearer / JWT / proxy-auth / secret / id tokens from text.

    The canonical sanitizer owns all credential and provider-identifier rules.
    """
    text = _canonical_sanitize_text(value)
    return _SESSION_ID_RE.sub("[REDACTED_STRIPE_ID]", text)


def build_payment_method_payload(
    contract: CheckoutRequestContract,
    checkout_session_id: str,
    publishable_key: str,
    identifiers: WalletFlowIdentifiers,
    *,
    billing_details: Mapping[str, Any] | None = None,
    time_on_page_ms: int = 30_000,
) -> dict[str, str]:
    billing = _billing_details(contract, billing_details)
    body = {
        "type": contract.stripe_payment_method,
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": billing["country"],
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][state]": billing["state"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "payment_user_agent": (
            f"stripe.js/{identifiers.runtime_version}; stripe-js-v3/{identifiers.runtime_version}; "
            "payment-element; deferred-intent"
        ),
        "referrer": "https://chatgpt.com",
        "time_on_page": str(max(0, int(time_on_page_ms))),
        "client_attribution_metadata[client_session_id]": identifiers.stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": checkout_session_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "key": publishable_key,
        "_stripe_version": contract.stripe_init_payload(publishable_key)["_stripe_version"],
    }
    return body


def build_confirm_payload(
    contract: CheckoutRequestContract,
    session: CheckoutSessionContract,
    init_payload: Mapping[str, Any],
    payment_method_id: str,
    identifiers: WalletFlowIdentifiers,
) -> dict[str, str]:
    evidence = StripeCapabilityEvidence.from_payload(init_payload, fallback_currency=contract.currency)
    hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    return_url = _confirm_return_url(hosted_url, session)
    return {
        "guid": uuid.uuid4().hex,
        "muid": uuid.uuid4().hex,
        "sid": uuid.uuid4().hex,
        "payment_method": payment_method_id,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "version": identifiers.runtime_version,
        "expected_amount": str(evidence.amount_minor if evidence.amount_minor is not None else 0),
        "expected_payment_method_type": contract.stripe_payment_method,
        "return_url": return_url,
        "elements_session_client[session_id]": identifiers.elements_session_id,
        "elements_session_client[locale]": contract.payment_locale,
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[stripe_js_id]": identifiers.stripe_js_id,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "client_attribution_metadata[client_session_id]": identifiers.stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session.checkout_session_id,
        "client_attribution_metadata[checkout_config_id]": str(init_payload.get("config_id") or ""),
        "client_attribution_metadata[elements_session_id]": identifiers.elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": identifiers.elements_session_config_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "consent[terms_of_service]": "accepted",
        "key": session.publishable_key,
        "_stripe_version": contract.stripe_init_payload(session.publishable_key)["_stripe_version"],
    }


def capability_result(evidence: StripeCapabilityEvidence, contract: CheckoutRequestContract) -> dict[str, Any]:
    classification, supported = evidence.classification_for(contract.stripe_payment_method)
    return {
        "classification": classification,
        "conclusive": supported is not None,
        "supported": supported,
        "amount_minor": evidence.amount_minor,
        "currency": evidence.currency,
        "currency_present": evidence.currency_present,
        "payment_method_types": list(evidence.payment_method_types),
        "ordered_payment_method_types": list(evidence.ordered_payment_method_types),
        "custom_payment_methods": list(evidence.custom_payment_methods),
        "offer_state": evidence.offer_state,
    }


def run_wallet_provider(
    payment_method: Any,
    access_token: str,
    transport: WalletProviderTransport,
    *,
    probe_only: bool = False,
    billing_details: Mapping[str, Any] | None = None,
    auth_context: Mapping[str, Any] | None = None,
    transport_context: Mapping[str, Any] | None = None,
    stripe_publishable_key: str = "",
    require_zero: bool = False,
    promotion_update: bool | None = None,
    max_approve_attempts: int = 6,
    max_poll_attempts: int = 25,
    poll_interval_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    uuid_factory: Callable[[], Any] = uuid.uuid4,
) -> dict[str, Any]:
    """Probe or execute a wallet redirect flow through an injected transport."""

    stage = "validation"
    spec: WalletMethodSpec | None = None
    capability: dict[str, Any] | None = None
    flow_id = str(uuid_factory())
    context = dict(auth_context or {})
    wire_context = dict(transport_context or {})
    try:
        spec = wallet_method_spec(payment_method)
        token = str(access_token or "").strip()
        if not token:
            raise WalletProviderError(
                "access_token is required",
                error_code="wallet_access_token_missing",
                error_stage="validation",
            )
        contract = CheckoutRequestContract.for_payment_method(spec.key)
        identifiers = WalletFlowIdentifiers.create(uuid_factory)

        stage = "checkout"
        checkout_payload = contract.checkout_payload()
        checkout_response = transport.create_checkout(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                checkout_payload,
                auth_context=context,
                transport_context=wire_context,
            )
        )
        session = CheckoutSessionContract.from_payload(
            checkout_response,
            billing_country=contract.billing_country,
            fallback_publishable_key=stripe_publishable_key,
        )

        if _promotion_update_enabled(spec.key, require_zero, promotion_update, wire_context):
            stage = "promotion"
            promotion_response = transport.update_checkout(
                _request(
                    stage,
                    spec,
                    contract,
                    flow_id,
                    token,
                    _promotion_payload(session, checkout_payload),
                    session=session,
                    auth_context=context,
                    transport_context=wire_context,
                )
            )
            _raise_for_promotion_payload(promotion_response)

        stage = "stripe_init"
        init_request_payload = contract.stripe_init_payload(
            session.publishable_key,
            stripe_js_id=identifiers.stripe_js_id,
        )
        init_payload = transport.stripe_init(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                init_request_payload,
                session=session,
                auth_context=context,
                transport_context=wire_context,
            )
        )
        evidence = StripeCapabilityEvidence.from_payload(init_payload, fallback_currency=contract.currency)
        capability = capability_result(evidence, contract)
        base_result = {
            "payment_method": spec.key,
            "capability": capability,
            "checkout_session_id_present": bool(session.checkout_session_id),
            "retryable": False,
            "error_stage": "",
        }
        if probe_only:
            return build_capability_probe_result(
                contract,
                evidence,
                checkout_session_present=bool(session.checkout_session_id),
                require_zero=require_zero,
                extra={**base_result, "probe_only": True, "url": ""},
            )
        if capability["supported"] is False:
            raise WalletProviderError(
                f"Stripe init does not offer {spec.label}",
                error_code="wallet_method_unavailable",
                error_stage="stripe_init",
            )
        if capability["supported"] is None:
            raise WalletProviderError(
                f"Stripe init did not provide conclusive {spec.label} capability evidence",
                error_code="wallet_capability_unknown",
                error_stage="stripe_init",
                retryable=False,
                status="unknown",
            )
        expected_currency = str(contract.currency or "").strip().upper()
        actual_currency = str(evidence.currency or "").strip().upper()
        if not evidence.currency_present:
            raise WalletProviderError(
                "Stripe init did not provide a currency for the wallet contract",
                error_code="wallet_checkout_currency_unknown",
                error_stage="stripe_init",
                retryable=False,
                status="unknown",
            )
        if expected_currency and actual_currency != expected_currency:
            raise WalletProviderError(
                f"wallet checkout currency mismatch: expected={expected_currency} actual={actual_currency}",
                error_code="wallet_checkout_currency_mismatch",
                error_stage="stripe_init",
                retryable=False,
            )
        # amount_minor is None 表示 Stripe 响应里取不到金额证据 —— 属于协议模糊，
        # 应交由上层当 unknown 处理，而不是用 Python 的 `None != 0 == True` 语义误判成
        # 非零报价、误杀一个可能可用的 0 元 checkout。
        if require_zero and evidence.amount_minor is not None and evidence.amount_minor != 0:
            raise WalletProviderError(
                f"wallet checkout is not zero due: amount={evidence.amount_minor} currency={evidence.currency}",
                error_code="wallet_checkout_not_zero_due",
                error_stage="stripe_init",
            )
        if require_zero and evidence.amount_minor is None:
            raise WalletProviderError(
                f"wallet checkout zero-due check inconclusive: amount not present in stripe init response",
                error_code="wallet_checkout_amount_unknown",
                error_stage="stripe_init",
                retryable=False,
                status="unknown",
            )

        stage = "payment_method"
        payment_method_payload = build_payment_method_payload(
            contract,
            session.checkout_session_id,
            session.publishable_key,
            identifiers,
            billing_details=billing_details,
        )
        payment_method_response = transport.create_payment_method(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                payment_method_payload,
                session=session,
                auth_context=context,
                transport_context=wire_context,
            )
        )
        payment_method_id = _payment_method_id(payment_method_response)

        stage = "confirm"
        confirm_payload = build_confirm_payload(contract, session, init_payload, payment_method_id, identifiers)
        confirm_response = transport.confirm_payment(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                confirm_payload,
                session=session,
                auth_context=context,
                transport_context=wire_context,
            )
        )
        _raise_for_terminal_payload(confirm_response, stage)
        redirect_url = _extract_redirect_url(confirm_response)

        stage = "approve"
        approve_payload = {
            "checkout_session_id": session.checkout_session_id,
            "processor_entity": session.processor_entity,
        }
        approved = False
        for attempt in range(1, max(1, int(max_approve_attempts)) + 1):
            approval_response = transport.approve_checkout(
                _request(
                    stage,
                    spec,
                    contract,
                    flow_id,
                    token,
                    approve_payload,
                    attempt=attempt,
                    session=session,
                    auth_context=context,
                    transport_context=wire_context,
                )
            )
            approval_state = _response_state(approval_response, prefer_result=True)
            if approval_state == "approved":
                approved = True
                redirect_url = redirect_url or _extract_redirect_url(approval_response)
                break
            _raise_for_terminal_payload(approval_response, stage)
            if attempt < max_approve_attempts:
                sleep(max(0.0, float(poll_interval_seconds)))
        if not approved:
            raise WalletUnknownResultError(
                "ChatGPT checkout approval did not become approved",
                error_stage=stage,
            )

        stage = "poll"
        poll_payload = contract.stripe_init_payload(
            session.publishable_key,
            stripe_js_id=identifiers.stripe_js_id,
        )
        for attempt in range(1, max(1, int(max_poll_attempts)) + 1):
            if redirect_url:
                break
            poll_response = transport.poll_payment(
                _request(
                    stage,
                    spec,
                    contract,
                    flow_id,
                    token,
                    poll_payload,
                    attempt=attempt,
                    session=session,
                    auth_context=context,
                    transport_context=wire_context,
                )
            )
            _raise_for_terminal_payload(poll_response, stage)
            redirect_url = _extract_redirect_url(poll_response)
            if not redirect_url and attempt < max_poll_attempts:
                sleep(max(0.0, float(poll_interval_seconds)))
        if not redirect_url:
            raise WalletUnknownResultError(
                "wallet redirect did not materialize before polling ended",
                error_stage=stage,
            )

        stage = "follow_redirect"
        follow_response = transport.follow_redirect(
            _request(
                stage,
                spec,
                contract,
                flow_id,
                token,
                {},
                session=session,
                auth_context=context,
                transport_context=wire_context,
                redirect_url=redirect_url,
            )
        )
        provider_url = _followed_url(follow_response) or redirect_url
        if not _is_provider_url(provider_url, spec):
            raise WalletUnknownResultError(
                f"redirect chain did not resolve to a recognized {spec.label} provider host",
                error_stage=stage,
            )
        return {
            **base_result,
            "ok": True,
            "status": "completed",
            "operation": "extract_link",
            "probe_only": False,
            "url": provider_url,
            "provider_redirect_url": provider_url,
            "link_type": f"{spec.key}_protocol",
        }
    except asyncio.CancelledError:
        return _failure_result(spec, WalletCancelledError(error_stage=stage), capability=capability)
    except concurrent.futures.CancelledError:
        return _failure_result(spec, WalletCancelledError(error_stage=stage), capability=capability)
    except Exception as exc:
        return _failure_result(spec, _structured_error(exc, stage), capability=capability)


def _request(
    stage: str,
    spec: WalletMethodSpec,
    contract: CheckoutRequestContract,
    flow_id: str,
    access_token: str,
    payload: Mapping[str, Any],
    *,
    attempt: int = 1,
    session: CheckoutSessionContract | None = None,
    auth_context: Mapping[str, Any],
    transport_context: Mapping[str, Any],
    redirect_url: str = "",
) -> WalletTransportRequest:
    return WalletTransportRequest(
        stage=stage,
        method=spec.key,
        contract=contract,
        flow_id=flow_id,
        attempt=attempt,
        checkout_session_id=session.checkout_session_id if session else "",
        processor_entity=session.processor_entity if session else "",
        access_token=access_token,
        publishable_key=session.publishable_key if session else "",
        payload=payload,
        auth_context=auth_context,
        transport_context=transport_context,
        redirect_url=redirect_url,
    )


def _promotion_update_enabled(
    payment_method: str,
    require_zero: bool,
    configured: bool | None,
    transport_context: Mapping[str, Any],
) -> bool:
    if payment_method != "gopay":
        return False
    if require_zero or configured is True:
        return True
    if configured is False:
        return False
    return any(
        str(transport_context.get(key) or "").strip()
        for key in ("promotion_proxy", "update_proxy")
    )


def _promotion_payload(
    session: CheckoutSessionContract,
    checkout_payload: Mapping[str, Any],
) -> dict[str, Any]:
    campaign = checkout_payload.get("promo_campaign")
    if not isinstance(campaign, Mapping):
        campaign = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }
    return {
        "checkout_session_id": session.checkout_session_id,
        "processor_entity": session.processor_entity,
        "plan_name": str(checkout_payload.get("plan_name") or "chatgptplusplan"),
        "price_interval": "month",
        "seat_quantity": 1,
        "promo_campaign": dict(campaign),
    }


def _raise_for_promotion_payload(payload: Mapping[str, Any]) -> None:
    _raise_for_terminal_payload(payload, "promotion")
    if payload.get("success") is False:
        raise WalletProviderError(
            "wallet checkout promotion update was rejected",
            error_code="wallet_promotion_rejected",
            error_stage="promotion",
            retryable=False,
        )


def _billing_details(
    contract: CheckoutRequestContract,
    supplied: Mapping[str, Any] | None,
) -> dict[str, str]:
    defaults = {
        "ID": {
            "name": "Budi Santoso",
            "email": "buyer@example.com",
            "line1": "Jl. Jend. Sudirman No. 1",
            "city": "Jakarta",
            "state": "DKI Jakarta",
            "postal_code": "10210",
        },
        "PH": {
            "name": "Miguel Santos",
            "email": "buyer@example.com",
            "line1": "6750 Ayala Avenue",
            "city": "Makati",
            "state": "Metro Manila",
            "postal_code": "1226",
        },
    }.get(contract.billing_country, {})
    merged = {**defaults, **{str(k): str(v) for k, v in dict(supplied or {}).items() if v is not None}}
    merged["country"] = contract.billing_country
    required = ("name", "email", "line1", "city", "state", "postal_code")
    missing = [key for key in required if not str(merged.get(key) or "").strip()]
    if missing:
        raise WalletProviderError(
            f"billing_details missing required fields: {', '.join(missing)}",
            error_code="wallet_billing_invalid",
            error_stage="payment_method",
        )
    return {key: str(value).strip() for key, value in merged.items()}


def _payment_method_id(response: Mapping[str, Any] | str) -> str:
    value = response if isinstance(response, str) else response.get("id") if isinstance(response, Mapping) else ""
    payment_method_id = str(value or "").strip()
    if not payment_method_id.startswith("pm_"):
        raise WalletProviderError(
            "Stripe payment method response did not contain a payment method id",
            error_code="wallet_payment_method_bad_response",
            error_stage="payment_method",
            retryable=True,
        )
    return payment_method_id


def _confirm_return_url(hosted_url: str, session: CheckoutSessionContract) -> str:
    if hosted_url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + hosted_url[len("https://checkout.stripe.com") :]
    if hosted_url.startswith("https://pay.openai.com"):
        return hosted_url
    return (
        f"https://pay.openai.com/c/pay/{session.checkout_session_id}"
        "?returned_from_redirect=true&ui_mode=custom"
    )


def _response_state(payload: Any, *, prefer_result: bool = False) -> str:
    if not isinstance(payload, Mapping):
        return ""
    keys = ("result", "state", "status") if prefer_result else ("state", "status", "result")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _raise_for_terminal_payload(payload: Any, stage: str) -> None:
    state = _response_state(payload)
    if state in _TERMINAL_CANCEL_STATES:
        raise WalletCancelledError(error_stage=stage)
    if state in _TERMINAL_FAILURE_STATES:
        raise WalletProviderError(
            f"wallet provider returned terminal state: {state}",
            error_code="wallet_provider_rejected",
            error_stage=stage,
        )
    if isinstance(payload, Mapping) and payload.get("error"):
        error = payload.get("error")
        message = error.get("message") if isinstance(error, Mapping) else error
        raise WalletProviderError(
            f"wallet provider error: {message or 'unknown error'}",
            error_code="wallet_provider_response_error",
            error_stage=stage,
            retryable=stage in _SIDE_EFFECT_STAGES,
            status="unknown" if stage in _SIDE_EFFECT_STAGES else "failed",
        )


def _extract_redirect_url(payload: Any, *, depth: int = 0) -> str:
    if depth > 10:
        return ""
    if isinstance(payload, str):
        return payload.strip() if _is_http_url(payload) else ""
    if isinstance(payload, Mapping):
        for key in ("provider_redirect_url", "final_url", "redirect_url", "url"):
            value = payload.get(key)
            if isinstance(value, str) and _is_http_url(value):
                return value.strip()
        for key, value in payload.items():
            if str(key).lower() in {"return_url", "success_url", "cancel_url", "stripe_hosted_url"}:
                continue
            found = _extract_redirect_url(value, depth=depth + 1)
            if found:
                return found
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found = _extract_redirect_url(value, depth=depth + 1)
            if found:
                return found
    return ""


def _followed_url(response: Mapping[str, Any] | str) -> str:
    return _extract_redirect_url(response)


def _is_http_url(value: Any) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.hostname)


def _is_provider_url(value: str, spec: WalletMethodSpec) -> bool:
    if not _is_http_url(value):
        return False
    host = str(urlsplit(value).hostname or "").lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in spec.redirect_hosts)


def _structured_error(exc: Exception, stage: str) -> WalletProviderError:
    if isinstance(exc, WalletProviderError):
        if (
            stage in _SIDE_EFFECT_STAGES
            and exc.status == "failed"
            and exc.error_code != "wallet_provider_rejected"
        ):
            return WalletProviderError(
                str(exc),
                error_code=exc.error_code,
                error_stage=exc.error_stage or stage,
                retryable=False,
                status="unknown",
            )
        return exc
    if isinstance(exc, CheckoutContractError):
        return WalletProviderError(
            str(exc),
            error_code=getattr(exc, "error_code", "checkout_contract_invalid"),
            error_stage=getattr(exc, "error_stage", stage),
            retryable=bool(getattr(exc, "retryable", False)),
        )
    if isinstance(exc, TimeoutError) and stage in _SIDE_EFFECT_STAGES:
        return WalletUnknownResultError(
            str(exc) or "wallet transport timed out after a side effect",
            error_stage=stage,
        )
    if isinstance(exc, TimeoutError):
        return WalletTimedOutError(str(exc) or "wallet transport timed out", error_stage=stage)
    status_code = _exception_status_code(exc)
    retryable = status_code == 429 or status_code >= 500 if status_code else not isinstance(exc, (TypeError, ValueError))
    # Once a side-effecting request starts, a local exception cannot prove that
    # the provider rejected it. Treat every unstructured failure as unknown.
    uncertain = stage in _SIDE_EFFECT_STAGES
    return WalletProviderError(
        str(exc) or exc.__class__.__name__,
        error_code="wallet_transport_error",
        error_stage=stage,
        retryable=False if uncertain else retryable,
        status="unknown" if uncertain else "failed",
    )


def _exception_status_code(exc: Exception) -> int:
    for value in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    return 0


def _failure_result(
    spec: WalletMethodSpec | None,
    error: WalletProviderError,
    *,
    capability: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "status": error.status,
        "payment_method": spec.key if spec else "",
        "url": "",
        "error": str(error),
        "error_code": error.error_code,
        "retryable": error.retryable,
        "error_stage": error.error_stage,
        "side_effect_started": error.error_stage in _SIDE_EFFECT_STAGES,
    }
    if capability is not None:
        result["capability"] = dict(capability)
    if error.status == "unknown":
        result["retryable"] = False
        result["requires_reconciliation"] = True
    return result


__all__ = [
    "WALLET_METHODS",
    "WalletCancelledError",
    "WalletFlowIdentifiers",
    "WalletMethodSpec",
    "WalletProviderError",
    "WalletProviderTransport",
    "WalletTimedOutError",
    "WalletTransportRequest",
    "WalletUnknownResultError",
    "build_confirm_payload",
    "build_payment_method_payload",
    "capability_result",
    "redact_sensitive_text",
    "run_wallet_provider",
    "wallet_method_spec",
]
