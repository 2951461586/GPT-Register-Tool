"""Non-CAPTCHA Stripe Hosted & ChatGPT Custom Checkout HTTP protocol entrypoints.

Split into cohesive sibling modules under ``sms_tool/payment/``:

* :mod:`.protocol_pay_common` — constants, :class:`ProtocolPaymentError`, card loading, pure helpers.
* :mod:`.protocol_pay_hosted` — :class:`HostedCheckoutProtocolClient` (Stripe ``cs_...``).
* :mod:`.protocol_pay_custom` — :class:`CustomCheckoutProtocolClient` (ChatGPT ``oaics_...``).

All public and helper names stay re-exported here for call-site and test compatibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .protocol_codec import (
    billing_currency_for,
    openai_internal_checkout_id,
    processor_from_checkout_url,
    stripe_checkout_id,
)
from .protocol_pay_common import (
    HOSTED_API_VERSION as HOSTED_API_VERSION,
    HOSTED_ELEMENTS_VERSION as HOSTED_ELEMENTS_VERSION,
    HOSTED_PAYMENT_UA as HOSTED_PAYMENT_UA,
    ProtocolPaymentError as ProtocolPaymentError,
    _billing_email as _billing_email,
    _custom_outcome as _custom_outcome,
    _hosted_stage as _hosted_stage,
    _local_device_ids as _local_device_ids,
    _new_device_id as _new_device_id,
    _obj_id as _obj_id,
    _resolve_billing as _resolve_billing,
    _safe_json as _safe_json,
    _stripe_error_fields as _stripe_error_fields,
    _stripe_error_from_intent as _stripe_error_from_intent,
    load_card_from_env_or_file as load_card_from_env_or_file,
)
from .protocol_pay_custom import CustomCheckoutProtocolClient as CustomCheckoutProtocolClient
from .protocol_pay_hosted import HostedCheckoutProtocolClient as HostedCheckoutProtocolClient


def quote_team_hosted_checkout(
    checkout_url: str,
    *,
    publishable_key: str,
    country: str = "US",
    currency: str = "",
    billing: Mapping[str, str] | None = None,
    proxy: str = "",
    timeout: int = 30,
    session: Any | None = None,
) -> dict[str, Any]:
    """Read-only quote helper for a Stripe Hosted Checkout (``cs_...``) URL/ID."""
    client = HostedCheckoutProtocolClient(proxy=proxy, timeout=timeout, session=session)
    resolved_billing = _resolve_billing(billing, country=country) if (billing or country) else None
    resolved_currency = str(currency or billing_currency_for(country or "US")).upper()
    return client.quote(
        session_id=checkout_url,
        publishable_key=publishable_key,
        billing=resolved_billing,
        currency=resolved_currency,
    )


def pay_team_hosted_checkout(
    checkout_url: str,
    *,
    publishable_key: str,
    card: Mapping[str, Any] | None = None,
    payment_method_id: str = "",
    country: str = "US",
    currency: str = "",
    billing: Mapping[str, str] | None = None,
    proxy: str = "",
    timeout: int = 30,
    max_poll_tries: int = 10,
    poll_interval_seconds: float = 1.0,
    fetch_timing: bool = True,
    session: Any | None = None,
) -> dict[str, Any]:
    """Execute a non-CAPTCHA Stripe Hosted Checkout card payment."""
    client = HostedCheckoutProtocolClient(proxy=proxy, timeout=timeout, session=session)
    return client.pay(
        checkout_url,
        publishable_key=publishable_key,
        card=card,
        payment_method_id=payment_method_id,
        billing=billing,
        country=country,
        currency=currency,
        max_poll_tries=max_poll_tries,
        poll_interval_seconds=poll_interval_seconds,
        fetch_timing=fetch_timing,
    )


def pay_custom_checkout(
    checkout_url: str,
    *,
    access_token: str,
    publishable_key: str,
    amount_minor: int = 0,
    currency: str = "USD",
    card: Mapping[str, Any] | None = None,
    billing: Mapping[str, str] | None = None,
    cookie_header: str = "",
    account_id: str = "",
    device_id: str = "",
    proxy: str = "",
    timeout: int = 30,
    read_only: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute or inspect a non-CAPTCHA ChatGPT ``oaics_`` Custom Checkout payment."""
    client = CustomCheckoutProtocolClient(
        access_token=access_token,
        cookie_header=cookie_header,
        proxy=proxy,
        timeout=timeout,
        account_id=account_id,
        device_id=device_id,
    )
    run_ctx = context if context is not None else {}
    spec = {
        "checkout_url": checkout_url,
        "checkout_id": openai_internal_checkout_id(checkout_url),
        "publishable_key": publishable_key,
        "currency": str(currency or "USD").upper(),
        "amount_minor": int(amount_minor),
        "processor": processor_from_checkout_url(checkout_url) or "openai_llc",
    }
    return client.run(run_ctx, spec, card=card, billing=billing, read_only=read_only)


def execute_protocol_card_payment(
    checkout_url: str,
    *,
    publishable_key: str,
    card: Mapping[str, Any] | None = None,
    payment_method_id: str = "",
    access_token: str = "",
    cookie_header: str = "",
    account_id: str = "",
    device_id: str = "",
    country: str = "US",
    currency: str = "",
    billing: Mapping[str, str] | None = None,
    proxy: str = "",
    timeout: int = 30,
    read_only: bool = False,
    quote_only: bool = False,
) -> dict[str, Any]:
    """Auto-route a Stripe Hosted (``cs_``) or ChatGPT Custom (``oaics_``) checkout."""
    cs_id = stripe_checkout_id(checkout_url)
    if cs_id:
        if quote_only or read_only:
            return quote_team_hosted_checkout(
                checkout_url,
                publishable_key=publishable_key,
                country=country,
                currency=currency,
                billing=billing,
                proxy=proxy,
                timeout=timeout,
            )
        return pay_team_hosted_checkout(
            checkout_url,
            publishable_key=publishable_key,
            card=card,
            payment_method_id=payment_method_id,
            country=country,
            currency=currency,
            billing=billing,
            proxy=proxy,
            timeout=timeout,
        )
    oaics_id = openai_internal_checkout_id(checkout_url)
    if oaics_id:
        if not access_token:
            raise ProtocolPaymentError(
                "ChatGPT oaics_ custom checkout requires an access_token",
                error_code="missing_access_token",
                error_stage="validation",
            )
        return pay_custom_checkout(
            checkout_url,
            access_token=access_token,
            publishable_key=publishable_key,
            currency=currency or billing_currency_for(country),
            card=card,
            billing=billing,
            cookie_header=cookie_header,
            account_id=account_id,
            device_id=device_id,
            proxy=proxy,
            timeout=timeout,
            read_only=read_only or quote_only,
        )
    raise ProtocolPaymentError(
        "checkout_url must contain either a Stripe cs_ or ChatGPT oaics_ session id",
        error_code="unsupported_checkout_url",
        error_stage="validation",
    )


__all__ = [
    "CustomCheckoutProtocolClient",
    "HOSTED_API_VERSION",
    "HOSTED_ELEMENTS_VERSION",
    "HOSTED_PAYMENT_UA",
    "HostedCheckoutProtocolClient",
    "ProtocolPaymentError",
    "execute_protocol_card_payment",
    "load_card_from_env_or_file",
    "pay_custom_checkout",
    "pay_team_hosted_checkout",
    "quote_team_hosted_checkout",
]
