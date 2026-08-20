"""Side-effect-limited Checkout and Stripe payment-method capability probe."""

from __future__ import annotations

import re
from typing import Any, Mapping, Protocol

from .checkout_contract import (
    CHECKOUT_PATH,
    CHECKOUT_URL,
    STRIPE_INIT_URL,
    CheckoutContractError,
    CheckoutRequestContract,
    CheckoutSessionContract,
    StripeCapabilityEvidence,
)


class PaymentCapabilityTransport(Protocol):
    def create_checkout(
        self,
        contract: CheckoutRequestContract,
        *,
        access_token: str,
        auth_context: dict[str, Any],
        proxy: str,
        timeout: int,
    ) -> CheckoutSessionContract: ...

    def stripe_init(
        self,
        contract: CheckoutRequestContract,
        checkout: CheckoutSessionContract,
        *,
        proxy: str,
        timeout: int,
    ) -> dict[str, Any]: ...


class CapabilityProbeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        error_stage: str,
        retryable: bool,
        status: str = "failed",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.error_stage = error_stage
        self.retryable = retryable
        self.status = status


class ChatGPTStripeCapabilityTransport:
    """Default wire transport; it never creates or confirms a payment method."""

    def create_checkout(
        self,
        contract: CheckoutRequestContract,
        *,
        access_token: str,
        auth_context: dict[str, Any],
        proxy: str,
        timeout: int,
    ) -> CheckoutSessionContract:
        from . import gen_pp_link

        cookie_header = str((auth_context or {}).get("cookie_header") or "")
        try:
            response = gen_pp_link._checkout_post(
                CHECKOUT_URL,
                contract.checkout_payload(),
                access_token,
                cookie_header,
                proxy,
                timeout,
                extra_headers={
                    "x-openai-target-path": CHECKOUT_PATH,
                    "x-openai-target-route": CHECKOUT_PATH,
                },
            )
        except Exception as exc:
            raise CapabilityProbeError(
                _safe_error(exc),
                error_code="checkout_transport_failed",
                error_stage="checkout_create",
                retryable=True,
                status="unknown",
            ) from exc
        payload = _response_json(
            response,
            stage="checkout_create",
            unauthorized_code="checkout_unauthorized",
            failure_code="checkout_failed",
        )
        try:
            return CheckoutSessionContract.from_payload(
                payload,
                billing_country=contract.billing_country,
                fallback_publishable_key=str(getattr(gen_pp_link, "DEFAULT_STRIPE_PK", "") or ""),
            )
        except CheckoutContractError as exc:
            raise CapabilityProbeError(
                str(exc),
                error_code=exc.error_code,
                error_stage=exc.error_stage,
                retryable=exc.retryable,
                status="unknown",
            ) from exc

    def stripe_init(
        self,
        contract: CheckoutRequestContract,
        checkout: CheckoutSessionContract,
        *,
        proxy: str,
        timeout: int,
    ) -> dict[str, Any]:
        from . import gen_pp_link

        try:
            session = gen_pp_link._new_session(proxy)
            response = session.post(
                STRIPE_INIT_URL.format(checkout_session_id=checkout.checkout_session_id),
                data=contract.stripe_init_payload(checkout.publishable_key),
                timeout=timeout,
            )
        except CheckoutContractError as exc:
            raise CapabilityProbeError(
                str(exc),
                error_code=exc.error_code,
                error_stage=exc.error_stage,
                retryable=exc.retryable,
                status="unknown",
            ) from exc
        except Exception as exc:
            raise CapabilityProbeError(
                _safe_error(exc),
                error_code="stripe_init_transport_failed",
                error_stage="stripe_init",
                retryable=True,
                status="unknown",
            ) from exc
        return _response_json(
            response,
            stage="stripe_init",
            unauthorized_code="stripe_init_unauthorized",
            failure_code="stripe_init_failed",
        )


def build_capability_probe_result(
    contract: CheckoutRequestContract,
    evidence: StripeCapabilityEvidence,
    *,
    checkout_session_present: bool,
    require_zero: bool,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the common, side-effect-free capability result contract."""
    classification, available = evidence.classification_for(contract.stripe_payment_method)
    reason = "payment_method_available" if available else "payment_method_unavailable"
    eligible: bool | None = available
    expected_currency = str(contract.currency or "").strip().upper()
    actual_currency = str(evidence.currency or "").strip().upper()
    if available and not evidence.currency_present:
        classification, eligible, reason = "unknown", None, "checkout_currency_unknown"
    elif available and expected_currency and actual_currency != expected_currency:
        classification, eligible, reason = "ineligible", False, "checkout_currency_mismatch"
    elif available and require_zero and evidence.amount_minor is None:
        classification, eligible, reason = "unknown", None, "checkout_amount_unknown"
    elif available and require_zero and evidence.amount_minor != 0:
        classification, eligible, reason = "ineligible", False, "nonzero_offer"
    elif available:
        classification, eligible, reason = "eligible", True, "payment_method_available"

    conclusive = classification in {"eligible", "ineligible"}
    result = dict(extra or {})
    result.update({
        "ok": conclusive,
        "operation": "payment_method_capability_probe",
        "payment_method": contract.payment_method,
        "status": "completed" if conclusive else "unknown",
        "classification": classification,
        "decision": reason,
        "eligible": eligible,
        "method_available": available,
        "conclusive": conclusive,
        "checkout_country": contract.billing_country,
        "currency": evidence.currency or contract.currency,
        "amount": evidence.amount_minor,
        "offer_state": evidence.offer_state,
        "payment_method_types": list(evidence.payment_method_types),
        "ordered_payment_method_types": list(evidence.ordered_payment_method_types),
        "custom_payment_methods": list(evidence.custom_payment_methods),
        "checkout_session_present": bool(checkout_session_present),
        "retryable": not conclusive,
        "error_stage": "",
        "error_code": "",
        "error": "",
    })
    if not conclusive:
        result.update({
            "error": reason,
            "error_code": reason,
            "error_stage": "capability_classification",
        })
    return result


def payment_method_capability_probe(
    access_token: str,
    payment_method: str,
    *,
    auth_context: dict[str, Any] | None = None,
    proxy: Any = None,
    checkout_proxy: Any = None,
    provider_proxy: Any = None,
    stripe_init_proxy: Any = None,
    promotion_proxy: Any = None,
    approve_proxy: Any = None,
    confirm_proxy: Any = None,
    checkout_country: str = "",
    billing_country: str = "",
    currency: str = "",
    payment_locale: str = "",
    browser_locale: str = "",
    browser_timezone: str = "",
    promo_campaign_id: str = "plus-1-month-free",
    checkout_ui_mode: str = "custom",
    require_zero: bool = True,
    stage_proxy_countries: Mapping[str, str] | None = None,
    timeout: int = 45,
    transport: PaymentCapabilityTransport | None = None,
    custom_payment_method_type_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Create Checkout and call Stripe init, stopping before payment-method creation."""
    method = str(payment_method or "").strip().lower().replace("-", "_")
    base = {
        "ok": False,
        "operation": "payment_method_capability_probe",
        "payment_method": method,
        "status": "unknown",
        "classification": "unknown",
        "eligible": None,
        "method_available": None,
        "conclusive": False,
        "retryable": False,
        "error_stage": "",
        "error_code": "",
        "error": "",
    }
    if not str(access_token or "").strip():
        return {
            **base,
            "status": "failed",
            "error": "access_token is required",
            "error_code": "missing_access_token",
            "error_stage": "validation",
        }
    if method == "paypal" and transport is None:
        return _paypal_capability_probe(
            access_token=str(access_token).strip(),
            auth_context=auth_context if isinstance(auth_context, dict) else {},
            checkout_proxy=_proxy_text(checkout_proxy or proxy),
            provider_proxy=_proxy_text(provider_proxy or proxy),
            stripe_init_proxy=_proxy_text(stripe_init_proxy or provider_proxy or proxy),
            payment_method_proxy=_proxy_text(checkout_proxy or provider_proxy or proxy),
            confirm_proxy=_proxy_text(confirm_proxy or provider_proxy or proxy),
            approve_proxy=_proxy_text(approve_proxy or provider_proxy or proxy),
            promotion_proxy=_proxy_text(promotion_proxy or provider_proxy or proxy),
            target_country=billing_country or checkout_country or "US",
            checkout_country=checkout_country or billing_country or "US",
            promo_campaign_id=promo_campaign_id,
            require_zero=require_zero,
            stage_proxy_countries=stage_proxy_countries,
            timeout=timeout,
        )
    if method == "gcash" and transport is None:
        from .gcash_provider import DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID, run_gcash_provider
        from .gcash_transport import ChatGPTGCashTransport

        return run_gcash_provider(
            str(access_token).strip(),
            ChatGPTGCashTransport(timeout=timeout),
            probe_only=True,
            auth_context=auth_context if isinstance(auth_context, dict) else {},
            transport_context={
                "default_proxy": _proxy_text(proxy),
                "checkout_proxy": _proxy_text(checkout_proxy or proxy),
                "provider_proxy": _proxy_text(checkout_proxy or provider_proxy or stripe_init_proxy or proxy),
                "promotion_proxy": _proxy_text(promotion_proxy or provider_proxy or proxy),
                "confirm_proxy": _proxy_text(confirm_proxy or checkout_proxy or provider_proxy or proxy),
            },
            custom_payment_method_type_id=(
                str(custom_payment_method_type_id or "").strip()
                or DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID
            ),
            require_zero=require_zero,
        )
    try:
        contract = CheckoutRequestContract.for_payment_method(
            method,
            billing_country=billing_country or checkout_country,
            currency=currency,
            payment_locale=payment_locale,
            browser_locale=browser_locale,
            browser_timezone=browser_timezone,
            promo_campaign_id=promo_campaign_id,
            checkout_ui_mode=checkout_ui_mode,
        )
        wire = transport or ChatGPTStripeCapabilityTransport()
        checkout = wire.create_checkout(
            contract,
            access_token=str(access_token).strip(),
            auth_context=auth_context if isinstance(auth_context, dict) else {},
            proxy=_proxy_text(checkout_proxy or proxy),
            timeout=max(5, int(timeout or 45)),
        )
        init_payload = wire.stripe_init(
            contract,
            checkout,
            proxy=_proxy_text(stripe_init_proxy or provider_proxy or proxy),
            timeout=max(5, int(timeout or 45)),
        )
        evidence = StripeCapabilityEvidence.from_payload(init_payload, fallback_currency=contract.currency)
        return build_capability_probe_result(
            contract,
            evidence,
            checkout_session_present=bool(checkout.checkout_session_id),
            require_zero=require_zero,
        )
    except CapabilityProbeError as exc:
        return {
            **base,
            "status": exc.status,
            "error": str(exc),
            "error_code": exc.error_code,
            "error_stage": exc.error_stage,
            "retryable": exc.retryable,
        }
    except CheckoutContractError as exc:
        return {
            **base,
            "status": "failed",
            "error": str(exc),
            "error_code": exc.error_code,
            "error_stage": exc.error_stage,
            "retryable": exc.retryable,
        }
    except Exception as exc:
        return {
            **base,
            "status": "unknown",
            "error": _safe_error(exc),
            "error_code": "capability_probe_unexpected",
            "error_stage": "capability_probe",
            "retryable": True,
        }


def _response_json(
    response: Any,
    *,
    stage: str,
    unauthorized_code: str,
    failure_code: str,
) -> dict[str, Any]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        retryable = status_code in {403, 408, 409, 425, 429} or status_code >= 500
        code = unauthorized_code if status_code in {401, 403} else failure_code
        raise CapabilityProbeError(
            f"{stage} returned HTTP {status_code}",
            error_code=code,
            error_stage=stage,
            retryable=retryable,
            status="unknown" if retryable else "failed",
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise CapabilityProbeError(
            f"{stage} returned invalid JSON",
            error_code=f"{failure_code}_bad_json",
            error_stage=stage,
            retryable=True,
            status="unknown",
        ) from exc
    if not isinstance(payload, dict):
        raise CapabilityProbeError(
            f"{stage} returned a non-object JSON payload",
            error_code=f"{failure_code}_bad_json",
            error_stage=stage,
            retryable=True,
            status="unknown",
        )
    return payload


def _proxy_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("https") or value.get("http") or ""
    return str(value or "").strip()


def _paypal_capability_probe(
    *,
    access_token: str,
    auth_context: dict[str, Any],
    checkout_proxy: str,
    provider_proxy: str,
    stripe_init_proxy: str,
    payment_method_proxy: str,
    confirm_proxy: str,
    approve_proxy: str,
    promotion_proxy: str,
    target_country: str,
    checkout_country: str,
    promo_campaign_id: str,
    require_zero: bool,
    stage_proxy_countries: Mapping[str, str] | None,
    timeout: int,
) -> dict[str, Any]:
    """Probe PayPal capability on a disposable Checkout before side effects.

    The probe deliberately applies the promo to its own Checkout before
    Stripe init. The production flow remains standard Checkout -> confirm ->
    approve -> promo; this probe only answers whether the account can reach a
    zero-due PayPal offer without creating a payment method or approval.
    """
    try:
        from .paypal_extract import PPLinkExtractor

        extractor = PPLinkExtractor(
            access_token=access_token,
            checkout_proxy=checkout_proxy,
            provider_proxy=provider_proxy,
            stripe_init_proxy=stripe_init_proxy,
            payment_method_proxy=payment_method_proxy,
            confirm_proxy=confirm_proxy,
            approve_proxy=approve_proxy,
            promotion_proxy=promotion_proxy,
            target_country=str(target_country or "US").upper(),
            checkout_country=str(checkout_country or target_country or "US").upper(),
            require_zero=bool(require_zero),
            promo_campaign_id=promo_campaign_id,
            stage_proxy_countries=dict(stage_proxy_countries or {}),
            max_stage_retries=1,
            max_checkout_retries=1,
            proxy_state=None,
            cookie_header=str(auth_context.get("cookie_header") or ""),
            device_id=str(auth_context.get("oai_did") or auth_context.get("device_id") or ""),
        )
        checkout = extractor._create_checkout()
        cs_id = str(checkout.get("cs_id") or "")
        entity = str(checkout.get("processor_entity") or "")
        if cs_id.startswith("oaics_"):
            return {
                "ok": True,
                "operation": "payment_method_capability_probe",
                "payment_method": "paypal",
                "status": "completed",
                "classification": "eligible",
                "decision": "paypal_capability_available",
                "eligible": True,
                "method_available": True,
                "conclusive": True,
                "checkout_country": extractor.checkout_country,
                "checkout_session_present": True,
                "probe_checkout_kind": "oaics",
                "retryable": False,
            }
        if not extractor.enable_promotion or not extractor._checkout_update_promotion(cs_id, entity):
            return {
                "ok": True,
                "operation": "payment_method_capability_probe",
                "payment_method": "paypal",
                "status": "completed",
                "classification": "ineligible",
                "decision": "promotion_unavailable",
                "eligible": False,
                "method_available": True,
                "conclusive": True,
                "checkout_country": extractor.checkout_country,
                "checkout_session_present": True,
                "retryable": False,
            }
        init = extractor._stripe_init(cs_id, enforce_zero=True)
        methods = [str(item).lower() for item in (init.get("payment_method_types") or [])]
        if methods and "paypal" not in methods:
            return {
                "ok": True,
                "operation": "payment_method_capability_probe",
                "payment_method": "paypal",
                "status": "completed",
                "classification": "ineligible",
                "decision": "payment_method_unavailable",
                "eligible": False,
                "method_available": False,
                "conclusive": True,
                "payment_method_types": methods,
                "checkout_country": extractor.checkout_country,
                "checkout_session_present": True,
                "retryable": False,
            }
        amount_info = _amount_from_init(init)
        return {
            "ok": True,
            "operation": "payment_method_capability_probe",
            "payment_method": "paypal",
            "status": "completed",
            "classification": "eligible",
            "decision": "paypal_zero_due_available",
            "eligible": True,
            "method_available": True,
            "conclusive": True,
            "amount": amount_info.get("amount"),
            "currency": amount_info.get("currency") or extractor.checkout_currency,
            "payment_method_types": methods,
            "checkout_country": extractor.checkout_country,
            "checkout_session_present": True,
            "retryable": False,
        }
    except Exception as exc:
        error_code = str(getattr(exc, "error_code", "") or "paypal_capability_probe_failed")
        error_stage = str(getattr(exc, "error_stage", "") or "capability_probe")
        return {
            "ok": False,
            "operation": "payment_method_capability_probe",
            "payment_method": "paypal",
            "status": "unknown" if bool(getattr(exc, "retryable", False)) else "failed",
            "classification": "unknown",
            "decision": error_code,
            "eligible": None,
            "method_available": None,
            "conclusive": False,
            "error": _safe_error(exc),
            "error_code": error_code,
            "error_stage": error_stage,
            "retryable": bool(getattr(exc, "retryable", False)),
        }


def _amount_from_init(init: Mapping[str, Any]) -> dict[str, Any]:
    from .pp_link_helpers import stripe_amount_details

    return stripe_amount_details(dict(init or {}))


def _safe_error(exc: BaseException) -> str:
    text = str(exc or type(exc).__name__)
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", text)
    return text[:500]
