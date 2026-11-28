"""Non-CAPTCHA Stripe Hosted & ChatGPT Custom Checkout HTTP protocol entrypoints.

Split into cohesive sibling modules under ``sms_tool/payment/``:

* :mod:`.protocol_pay_common` — constants, :class:`ProtocolPaymentError`, card loading, pure helpers.
* :mod:`.protocol_pay_hosted` — :class:`HostedCheckoutProtocolClient` (Stripe ``cs_...``).
* :mod:`.protocol_pay_custom` — :class:`CustomCheckoutProtocolClient` (ChatGPT ``oaics_...``).

All public and helper names stay re-exported here for call-site and test compatibility.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..operation_journal import PaymentOperationConflict, PaymentOperationStore, conflict_result
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
    on_confirm_start: Callable[[], None] | None = None,
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
        on_confirm_start=on_confirm_start,
    )


def pay_custom_checkout(
    checkout_url: str,
    *,
    access_token: str,
    publishable_key: str,
    amount_minor: int = 0,
    currency: str = "USD",
    country: str = "US",
    card: Mapping[str, Any] | None = None,
    billing: Mapping[str, str] | None = None,
    cookie_header: str = "",
    account_id: str = "",
    device_id: str = "",
    proxy: str = "",
    timeout: int = 30,
    read_only: bool = False,
    context: dict[str, Any] | None = None,
    max_poll_tries: int = 5,
    poll_interval_seconds: float = 1.0,
    sleep_fn: Callable[[float], None] | None = None,
    on_confirm_start: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Execute or inspect a non-CAPTCHA ChatGPT ``oaics_`` Custom Checkout payment."""
    client = CustomCheckoutProtocolClient(
        access_token=access_token,
        cookie_header=cookie_header,
        proxy=proxy,
        timeout=timeout,
        account_id=account_id,
        device_id=device_id,
        sleep_fn=sleep_fn,
        max_poll_tries=max_poll_tries,
        poll_interval_seconds=poll_interval_seconds,
        on_confirm_start=on_confirm_start,
    )
    run_ctx = context if context is not None else {}
    resolved_country = str((billing or {}).get("country") or country or "US").strip().upper() or "US"
    spec = {
        "checkout_url": checkout_url,
        "checkout_id": openai_internal_checkout_id(checkout_url),
        "publishable_key": publishable_key,
        "currency": str(currency or billing_currency_for(resolved_country)).upper(),
        "country": resolved_country,
        "amount_minor": int(amount_minor),
        "processor": processor_from_checkout_url(checkout_url) or "openai_llc",
    }
    return client.run(run_ctx, spec, card=card, billing=billing, read_only=read_only)


def _journal_finish_payload(result: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    state = str(result.get("state") or "").strip().lower()
    if state == "paid" or result.get("ok") is True:
        return {
            "ok": True,
            "status": "completed",
            "payment_method": mode,
            "operation": "protocol_card_pay",
            "side_effect_started": True,
            "requires_reconciliation": False,
            "retryable": False,
        }
    if state in {"unknown", "pending"} or result.get("requires_reconciliation") is True:
        return {
            "ok": False,
            "status": "unknown",
            "payment_method": mode,
            "operation": "protocol_card_pay",
            "error": str(result.get("message") or result.get("error") or "payment outcome unknown"),
            "error_code": str(result.get("code") or result.get("error_code") or "payment_outcome_unknown"),
            "error_stage": str(result.get("stage") or result.get("error_stage") or "confirm"),
            "side_effect_started": True,
            "requires_reconciliation": True,
            "retryable": False,
        }
    return {
        "ok": False,
        "status": "failed",
        "payment_method": mode,
        "operation": "protocol_card_pay",
        "error": str(result.get("message") or result.get("error") or "protocol card payment failed"),
        "error_code": str(result.get("code") or result.get("error_code") or state or "card_payment_failed"),
        "error_stage": str(result.get("stage") or result.get("error_stage") or "payment"),
        "side_effect_started": False,
        "requires_reconciliation": False,
        "retryable": True,
    }


def _run_with_operation_journal(
    session_id: str,
    *,
    mode: str,
    checkout_url: str,
    runtime_config: Mapping[str, Any] | None,
    operation_id: str,
    idempotency_key: str,
    runner: Callable[[Callable[[], None]], dict[str, Any]],
) -> dict[str, Any]:
    if runtime_config is None and not idempotency_key:
        return runner(lambda: None)
    key = str(idempotency_key or f"protocol_card_pay:{session_id}").strip()
    try:
        op = PaymentOperationStore.from_config(runtime_config or {}).begin(
            payment_method=mode,
            operation="protocol_card_pay",
            idempotency_key=key,
            operation_id=operation_id,
        )
    except PaymentOperationConflict as exc:
        conflict = conflict_result(exc)
        return {
            **conflict,
            "mode": mode,
            "session_id": session_id,
            "checkout_url": checkout_url,
            "state": "unknown" if conflict.get("requires_reconciliation") else "failed",
            "message": str(conflict.get("error") or "payment operation replay blocked"),
        }

    confirm_started = False
    finished = False

    def mark_confirm_started() -> None:
        nonlocal confirm_started
        confirm_started = True
        op.checkpoint("confirm", "running", side_effect_started=True)

    try:
        result = runner(mark_confirm_started)
        op.finish(_journal_finish_payload(result, mode=mode))
        finished = True
        return {
            **result,
            "operation_id": op.operation_id,
            "idempotency_key_hash": op.idempotency_key_hash,
        }
    finally:
        if not finished:
            if confirm_started:
                op.fail_unknown("confirm", "protocol_card_pay_aborted")
            else:
                op.finish(
                    {
                        "ok": False,
                        "status": "failed",
                        "payment_method": mode,
                        "operation": "protocol_card_pay",
                        "error": "protocol card payment aborted before confirm",
                        "error_code": "protocol_card_pay_aborted",
                        "error_stage": "setup",
                        "side_effect_started": False,
                        "requires_reconciliation": False,
                        "retryable": True,
                    }
                )


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
    runtime_config: Mapping[str, Any] | None = None,
    operation_id: str = "",
    idempotency_key: str = "",
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
        return _run_with_operation_journal(
            cs_id,
            mode="hosted_checkout",
            checkout_url=checkout_url,
            runtime_config=runtime_config,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            runner=lambda on_confirm: pay_team_hosted_checkout(
                checkout_url,
                publishable_key=publishable_key,
                card=card,
                payment_method_id=payment_method_id,
                country=country,
                currency=currency,
                billing=billing,
                proxy=proxy,
                timeout=timeout,
                on_confirm_start=on_confirm,
            ),
        )
    oaics_id = openai_internal_checkout_id(checkout_url)
    if oaics_id:
        if not access_token:
            raise ProtocolPaymentError(
                "ChatGPT oaics_ custom checkout requires an access_token",
                error_code="missing_access_token",
                error_stage="validation",
            )
        is_ro = read_only or quote_only
        if not is_ro and str(payment_method_id or "").strip() and card is None:
            raise ProtocolPaymentError(
                "ChatGPT oaics_ custom checkout uses Stripe confirmation tokens and does not accept an existing pm_ payment_method_id",
                error_code="oaics_payment_method_id_unsupported",
                error_stage="validation",
            )
        if is_ro:
            return pay_custom_checkout(
                checkout_url,
                access_token=access_token,
                publishable_key=publishable_key,
                country=country,
                currency=currency or billing_currency_for(country),
                card=card,
                billing=billing,
                cookie_header=cookie_header,
                account_id=account_id,
                device_id=device_id,
                proxy=proxy,
                timeout=timeout,
                read_only=True,
            )
        return _run_with_operation_journal(
            oaics_id,
            mode="custom_checkout",
            checkout_url=checkout_url,
            runtime_config=runtime_config,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            runner=lambda on_confirm: pay_custom_checkout(
                checkout_url,
                access_token=access_token,
                publishable_key=publishable_key,
                country=country,
                currency=currency or billing_currency_for(country),
                card=card,
                billing=billing,
                cookie_header=cookie_header,
                account_id=account_id,
                device_id=device_id,
                proxy=proxy,
                timeout=timeout,
                read_only=False,
                on_confirm_start=on_confirm,
            ),
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
