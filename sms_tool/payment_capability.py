"""Side-effect-limited Checkout and Stripe payment-method capability probe."""

from __future__ import annotations

import re
from typing import Any, Mapping, Protocol

from . import endpoints
from .checkout_contract import (
    CHECKOUT_PATH,
    CHECKOUT_URL,
    PLUS_TRIAL_CAMPAIGN_ID,
    STRIPE_INIT_URL,
    CheckoutContractError,
    CheckoutRequestContract,
    CheckoutSessionContract,
    StripeCapabilityEvidence,
)

#: ChatGPT ``payments/payment_methods`` route (path half; host comes from the
#: ``endpoints`` authority so the literal is not re-declared here).
_PAYMENT_METHODS_PATH = "/backend-api/payments/payment_methods"


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
        http_status: int = 0,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.error_stage = error_stage
        self.retryable = retryable
        self.status = status
        self.http_status = http_status


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
        device_id = str((auth_context or {}).get("device_id") or (auth_context or {}).get("oai_did") or "").strip()
        headers = {
            "x-openai-target-path": CHECKOUT_PATH,
            "x-openai-target-route": CHECKOUT_PATH,
        }
        if device_id:
            headers["OAI-Device-Id"] = device_id
        try:
            response = gen_pp_link._checkout_post(
                CHECKOUT_URL,
                contract.checkout_payload(),
                access_token,
                cookie_header,
                proxy,
                timeout,
                extra_headers=headers,
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

    def custom_checkout_session(
        self,
        contract: CheckoutRequestContract,
        checkout: CheckoutSessionContract,
        *,
        access_token: str,
        auth_context: dict[str, Any],
        proxy: str,
        timeout: int,
    ) -> dict[str, Any]:
        from . import gen_pp_link

        processor = checkout.processor_entity
        if not processor or not processor.replace("_", "").isalpha():
            raise CapabilityProbeError(
                "custom checkout processor invalid",
                error_code="checkout_session_invalid",
                error_stage="checkout_response",
                retryable=False,
            )
        try:
            cookie_header = str(auth_context.get("cookie_header") or "")
            device_id = str(auth_context.get("device_id") or auth_context.get("oai_did") or "").strip()
            response = gen_pp_link._checkout_get(
                f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{checkout.checkout_session_id}",
                access_token,
                cookie_header,
                proxy,
                timeout,
                extra_headers={"OAI-Device-Id": device_id} if device_id else None,
            )
            return _response_json(
                response,
                stage="custom_checkout",
                unauthorized_code="custom_checkout_unauthorized",
                failure_code="custom_checkout_failed",
            )
        except CapabilityProbeError:
            raise
        except Exception:
            raise CapabilityProbeError(
                "custom checkout transport failed",
                error_code="custom_checkout_transport_failed",
                error_stage="custom_checkout",
                retryable=True,
                status="unknown",
            ) from None

    def payment_methods_signal(
        self,
        *,
        access_token: str,
        auth_context: dict[str, Any],
        proxy: str,
        timeout: int,
    ) -> dict[str, Any]:
        """Read-only trial/method signal, independent of Checkout create.

        The create gate can refuse a probe (``400 unusual activity``), but this
        GET still answers ``one_click_trial_eligible``. It never creates,
        confirms or charges anything.
        """
        return _read_payment_methods_signal(
            access_token=access_token,
            auth_context=auth_context,
            proxy=proxy,
            timeout=timeout,
        )

    def stripe_elements(
        self,
        contract: CheckoutRequestContract,
        checkout: CheckoutSessionContract,
        init_payload: dict[str, Any],
        *,
        proxy: str,
        timeout: int,
    ) -> dict[str, Any]:
        from . import gen_pp_link

        evidence = StripeCapabilityEvidence.from_payload(init_payload)
        if evidence.amount_minor is None or not evidence.currency_present:
            return {}
        session = gen_pp_link._new_session(proxy)
        try:
            params = {
                "checkout_session_id": checkout.checkout_session_id,
                "key": checkout.publishable_key,
                "currency": evidence.currency.lower(),
                "locale": contract.payment_locale,
                "type": "deferred_intent",
                "deferred_intent[mode]": "subscription",
                "deferred_intent[amount]": str(evidence.amount_minor),
                "deferred_intent[currency]": evidence.currency.lower(),
                "_stripe_version": contract.stripe_init_payload(checkout.publishable_key)["_stripe_version"],
            }
            for index, method in enumerate(evidence.payment_method_types):
                params[f"deferred_intent[payment_method_types][{index}]"] = method
            response = session.get("https://api.stripe.com/v1/elements/sessions", params=params, timeout=timeout)
            return _response_json(
                response,
                stage="stripe_elements",
                unauthorized_code="stripe_elements_unauthorized",
                failure_code="stripe_elements_failed",
            )
        finally:
            session.close()


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
    if (
        evidence.payment_method_types
        and evidence.currency_present
        and expected_currency
        and actual_currency != expected_currency
        and not available
    ):
        classification, eligible, reason = "unknown", None, "checkout_currency_mismatch"
    elif available and not evidence.currency_present:
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
    result.update(
        {
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
        }
    )
    if not conclusive:
        result.update(
            {
                "error": reason,
                "error_code": reason,
                "error_stage": "capability_classification",
            }
        )
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
    promo_campaign_id: str = PLUS_TRIAL_CAMPAIGN_ID,
    checkout_ui_mode: str = "custom",
    require_zero: bool = True,
    stage_proxy_countries: Mapping[str, str] | None = None,
    timeout: int = 45,
    transport: PaymentCapabilityTransport | None = None,
    fallback_transport: PaymentCapabilityTransport | None = None,
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
                str(custom_payment_method_type_id or "").strip() or DEFAULT_GCASH_CUSTOM_PAYMENT_METHOD_ID
            ),
            require_zero=require_zero,
        )
    signal: dict[str, Any] = {}
    fallback_used = False
    checkout_proxy_text = _proxy_text(checkout_proxy or proxy)
    try:
        checkout_timeout = max(5, int(timeout or 45))
    except (TypeError, ValueError):
        checkout_timeout = 45
    try:
        context = dict(auth_context) if isinstance(auth_context, dict) else {}
        signal_reader = getattr(transport or ChatGPTStripeCapabilityTransport(), "payment_methods_signal", None)
        device_id = str(context.get("device_id") or "").strip()
        oai_did = str(context.get("oai_did") or "").strip()
        cookie_did = next(
            (
                part.partition("=")[2].strip().strip('"')
                for part in str(context.get("cookie_header") or "").split(";")
                if part.partition("=")[0].strip().lower() == "oai-did"
            ),
            "",
        )
        if len({value for value in (device_id, oai_did, cookie_did) if value}) > 1:
            raise CapabilityProbeError(
                "Checkout device identity conflicts with account cookie",
                error_code="checkout_identity_mismatch",
                error_stage="checkout_create",
                retryable=False,
            )
        if not device_id and (oai_did or cookie_did):
            context["device_id"] = oai_did or cookie_did
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
        wire: PaymentCapabilityTransport = transport or ChatGPTStripeCapabilityTransport()
        if callable(signal_reader):
            try:
                raw_signal = signal_reader(
                    access_token=str(access_token).strip(),
                    auth_context=context,
                    proxy=_proxy_text(checkout_proxy or proxy),
                    timeout=max(5, int(timeout or 45)),
                )
            except Exception:
                raw_signal = {}
            if isinstance(raw_signal, Mapping):
                signal = dict(raw_signal)
        try:
            checkout = wire.create_checkout(
                contract,
                access_token=str(access_token).strip(),
                auth_context=context,
                proxy=checkout_proxy_text,
                timeout=checkout_timeout,
            )
        except CapabilityProbeError as primary_error:
            # A browser/external session can still open the Checkout the
            # protocol transport was refused. Only risk-block and transport
            # failures fall through; a deterministic contract error does not.
            if fallback_transport is None:
                raise
            if not _should_try_fallback(primary_error):
                raise
            checkout = fallback_transport.create_checkout(
                contract,
                access_token=str(access_token).strip(),
                auth_context=context,
                proxy=checkout_proxy_text,
                timeout=checkout_timeout,
            )
            wire = fallback_transport
            fallback_used = True
        evidence_sources: list[str] = []
        if checkout.checkout_session_id.startswith("oaics_"):
            custom_reader = getattr(wire, "custom_checkout_session", None)
            if custom_reader is None:
                raise CapabilityProbeError(
                    "custom checkout reader unavailable",
                    error_code="custom_checkout_unavailable",
                    error_stage="custom_checkout",
                    retryable=False,
                    status="unknown",
                )
            init_payload = custom_reader(
                contract,
                checkout,
                access_token=str(access_token).strip(),
                auth_context=context,
                proxy=_proxy_text(checkout_proxy or proxy),
                timeout=max(5, int(timeout or 45)),
            )
            if StripeCapabilityEvidence.from_payload(init_payload).payment_method_types:
                evidence_sources.append("custom_checkout")
            checkout_kind = "oaics"
        else:
            init_payload = wire.stripe_init(
                contract,
                checkout,
                proxy=_proxy_text(stripe_init_proxy or provider_proxy or proxy),
                timeout=max(5, int(timeout or 45)),
            )
            if StripeCapabilityEvidence.from_payload(init_payload).payment_method_types:
                evidence_sources.append("stripe_init")
            elements = {}
            elements_reader = getattr(wire, "stripe_elements", None)
            if elements_reader is not None:
                try:
                    elements = elements_reader(
                        contract,
                        checkout,
                        init_payload,
                        proxy=_proxy_text(stripe_init_proxy or provider_proxy or proxy),
                        timeout=min(12, max(5, int(timeout or 45))),
                    )
                except Exception:
                    # Elements is supplemental. An explicit init method remains
                    # evidence even if the optional session read fails.
                    elements = {}
            if isinstance(elements, dict) and StripeCapabilityEvidence.from_payload(elements).payment_method_types:
                evidence_sources.append("stripe_elements")
                init_payload = {"init": init_payload, "elements": elements}
            checkout_kind = "stripe"
        evidence = StripeCapabilityEvidence.from_payload(init_payload, fallback_currency=contract.currency)
        result = build_capability_probe_result(
            contract,
            evidence,
            checkout_session_present=bool(checkout.checkout_session_id),
            require_zero=require_zero,
        )
        result["checkout_kind"] = checkout_kind
        result["evidence_sources"] = evidence_sources
        result["fallback_used"] = fallback_used
        if signal:
            result["payment_methods_signal"] = signal
            if "one_click_trial_eligible" in signal:
                result["one_click_trial_eligible"] = signal["one_click_trial_eligible"]
        return result
    except CapabilityProbeError as exc:
        result = {
            **base,
            "status": exc.status,
            "error": str(exc),
            "error_code": exc.error_code,
            "error_stage": exc.error_stage,
            "retryable": exc.retryable,
        }
        if exc.http_status:
            result["http_status"] = exc.http_status
        result["fallback_used"] = fallback_used
        if signal:
            result["payment_methods_signal"] = signal
        return result
    except CheckoutContractError as exc:
        return {
            **base,
            "status": "failed",
            "error": str(exc),
            "error_code": exc.error_code,
            "error_stage": exc.error_stage,
            "retryable": exc.retryable,
            **({"payment_methods_signal": signal} if signal else {}),
        }
    except Exception as exc:
        return {
            **base,
            "status": "unknown",
            "error": _safe_error(exc),
            "error_code": "capability_probe_unexpected",
            "error_stage": "capability_probe",
            "retryable": True,
            **({"payment_methods_signal": signal} if signal else {}),
        }


def _response_json(
    response: Any,
    *,
    stage: str,
    unauthorized_code: str,
    failure_code: str,
) -> dict[str, Any]:
    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code >= 400:
        retryable = status_code in {403, 408, 409, 425, 429} or status_code >= 500
        code = unauthorized_code if status_code in {401, 403} else failure_code
        # Only recognize fixed, non-sensitive vendor signals. Never echo the
        # response body or arbitrary upstream codes into account records.
        if stage == "checkout_create" and status_code in {400, 429}:
            try:
                body = response.json()
            except Exception:
                body = {}
            detail = body.get("detail") if isinstance(body, dict) else None
            if (
                status_code == 429
                and isinstance(detail, dict)
                and detail.get("code") == "checkout_creation_rate_limited"
            ):
                code = "checkout_creation_rate_limited"
            elif status_code == 400 and isinstance(detail, str) and "unusual activity" in detail.lower():
                code = "checkout_risk_blocked"
        raise CapabilityProbeError(
            f"{stage} returned HTTP {status_code}",
            error_code=code,
            error_stage=stage,
            retryable=retryable,
            status="unknown" if retryable else "failed",
            http_status=status_code,
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


def _read_payment_methods_signal(
    *,
    access_token: str,
    auth_context: dict[str, Any],
    proxy: str,
    timeout: int,
) -> dict[str, Any]:
    """Read-only ``one_click_trial_eligible`` / method signal from ChatGPT.

    Independent of Checkout create: it survives a refused create and never
    creates, confirms or charges anything. Every failure degrades to ``{}`` so
    it can only add evidence, never change a probe outcome.
    """
    from . import gen_pp_link

    token = str(access_token or "").strip()
    if not token:
        return {}
    context = auth_context if isinstance(auth_context, dict) else {}
    cookie_header = str(context.get("cookie_header") or "")
    device_id = str(context.get("device_id") or context.get("oai_did") or "").strip()
    extra_headers = {
        "x-openai-target-path": _PAYMENT_METHODS_PATH,
        "x-openai-target-route": _PAYMENT_METHODS_PATH,
    }
    if device_id:
        extra_headers["OAI-Device-Id"] = device_id
    try:
        response = gen_pp_link._checkout_get(
            endpoints.CHATGPT_PAYMENT_METHODS,
            token,
            cookie_header,
            _proxy_text(proxy),
            max(5, int(timeout or 45)),
            extra_headers=extra_headers,
        )
    except Exception:
        return {}
    try:
        status_code = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code >= 400:
        return {}
    try:
        payload = response.json()
    except Exception:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    signal: dict[str, Any] = {}
    trial = payload.get("one_click_trial_eligible")
    if isinstance(trial, bool):
        signal["one_click_trial_eligible"] = trial
    methods = payload.get("payment_method_types") or payload.get("payment_methods")
    if isinstance(methods, list):
        tokens = [str(item).strip() for item in methods if str(item).strip()]
        if tokens:
            signal["payment_method_types"] = tokens
    return signal


def _should_try_fallback(exc: CapabilityProbeError) -> bool:
    """True for the risk-block/transport failures a browser session can bypass."""
    return exc.error_code == "checkout_risk_blocked" or bool(exc.retryable)


def _paypal_probe_failure(exc: BaseException) -> dict[str, Any]:
    """Normalise a failed PayPal capability probe into the shared result shape.

    Extracted from the ``except`` handler so the handler itself stays free of
    boolean operators (``no-boolean-in-except`` scans the whole clause body).
    """
    error_code = str(getattr(exc, "error_code", "") or "paypal_capability_probe_failed")
    error_stage = str(getattr(exc, "error_stage", "") or "capability_probe")
    retryable = bool(getattr(exc, "retryable", False))
    return {
        "ok": False,
        "operation": "payment_method_capability_probe",
        "payment_method": "paypal",
        "status": "unknown" if retryable else "failed",
        "classification": "unknown",
        "decision": error_code,
        "eligible": None,
        "method_available": None,
        "conclusive": False,
        "error": _safe_error(exc),
        "error_code": error_code,
        "error_stage": error_stage,
        "retryable": retryable,
    }


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
        if not extractor.enable_promotion or not extractor.checkout_update_promotion(cs_id, entity):
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
        return _paypal_probe_failure(exc)


def _amount_from_init(init: Mapping[str, Any]) -> dict[str, Any]:
    from .pp_link_helpers import stripe_amount_details

    return stripe_amount_details(dict(init or {}))


def _safe_error(exc: BaseException) -> str:
    text = str(exc or type(exc).__name__)
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", text)
    return text[:500]
