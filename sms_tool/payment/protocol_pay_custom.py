"""Non-CAPTCHA ChatGPT Custom Checkout (oaics_...) protocol client."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit

from .checkout_contract import (
    CHATGPT_CHECKOUT_API,
    CHATGPT_CHECKOUT_CONFIRM,
    CHATGPT_CHECKOUT_SESSION_ACK,
    CHATGPT_CHECKOUT_TAXES,
    CHATGPT_CHECKOUT_UPDATE_TAX_ID,
    CHATGPT_SUBSCRIPTIONS,
    DEFAULT_STRIPE_VERSION,
    STRIPE_CONFIRMATION_TOKENS,
    STRIPE_ELEMENTS_SESSIONS,
    STRIPE_PAYMENT_INTENTS,
    STRIPE_SETUP_INTENTS,
)
from .protocol_codec import (
    decline_message,
    expected_amount_minor,
    minor_units_from_amount,
    normalize_card,
    openai_internal_checkout_id,
    payment_intent_from_confirm,
    processor_from_checkout_url,
    scrub_card_secrets,
)
from .protocol_pay_common import (
    ProtocolPaymentError,
    _CARD_SECRET_KEYS,
    _CHATGPT_HOST,
    _billing_email,
    _custom_outcome,
    _obj_id,
    _resolve_billing,
    _safe_json,
    _stripe_error_fields,
    _stripe_error_from_intent,
)
from .wire import _checkout_get, _checkout_post, _new_session


class CustomCheckoutProtocolClient:
    """Non-CAPTCHA ChatGPT Custom Checkout (``oaics_...``) protocol client.

    Uses ``sms_tool.payment.wire`` (``_checkout_get`` / ``_checkout_post``) for
    ChatGPT endpoints so native Sentinel & attestation headers apply automatically,
    and ``_new_session`` for Stripe API calls.
    """

    def __init__(
        self,
        *,
        access_token: str,
        cookie_header: str = "",
        proxy: str = "",
        timeout: int = 30,
        account_id: str = "",
        device_id: str = "",
        stripe_session: Any | None = None,
        checkout_get_fn: Callable[..., Any] | None = None,
        checkout_post_fn: Callable[..., Any] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        max_poll_tries: int = 5,
        poll_interval_seconds: float = 1.0,
        on_confirm_start: Callable[[], None] | None = None,
    ) -> None:
        self.access_token = str(access_token or "").strip()
        self.cookie_header = str(cookie_header or "").strip()
        self.proxy = str(proxy or "").strip()
        self.timeout = max(5, int(timeout or 30))
        self.account_id = str(account_id or "").strip()
        self.device_id = str(device_id or "").strip()
        self.stripe_http = stripe_session if stripe_session is not None else _new_session(self.proxy)
        self._checkout_get = checkout_get_fn or _checkout_get
        self._checkout_post = checkout_post_fn or _checkout_post
        self._sleep = sleep_fn or time.sleep
        self._max_poll_tries = max(1, int(max_poll_tries))
        self._poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._on_confirm_start = on_confirm_start

    @staticmethod
    def _outcome(
        context: Mapping[str, Any],
        state: str,
        stage: str,
        message: str,
        **extra: Any,
    ) -> dict[str, Any]:
        raw_amount = context.get("amount_minor")
        amount_minor = int(raw_amount) if isinstance(raw_amount, int) and not isinstance(raw_amount, bool) else None
        return _custom_outcome(
            state,
            stage,
            message,
            session_id=str(context.get("checkout_id") or ""),
            checkout_url=str(extra.pop("checkout_url", "") or context.get("checkout_url") or ""),
            amount_minor=amount_minor,
            currency=str(context.get("currency") or ""),
            **extra,
        )

    def _route_headers(self, route: str, *, page_url: str) -> dict[str, str]:
        headers = {
            "Referer": page_url,
            "x-openai-target-path": route,
            "x-openai-target-route": route,
        }
        if self.account_id:
            headers["Chatgpt-Account-Id"] = self.account_id
        if self.device_id:
            headers["OAI-Device-Id"] = self.device_id
        return headers

    @staticmethod
    def _validated_page_url(context: Mapping[str, Any]) -> str:
        url = str(context.get("checkout_url") or "")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != _CHATGPT_HOST:
            raise ProtocolPaymentError(
                "checkout_url does not belong to ChatGPT",
                error_code="checkout_origin_invalid",
                error_stage="validation",
            )
        return url

    def _read_checkout(self, context: dict[str, Any]) -> dict[str, Any]:
        page_url = self._validated_page_url(context)
        route = f"/backend-api/payments/checkout/{context['processor']}/{context['checkout_id']}"
        url = f"{CHATGPT_CHECKOUT_API}/{context['processor']}/{context['checkout_id']}"
        resp = self._checkout_get(
            url,
            self.access_token,
            self.cookie_header,
            self.proxy,
            self.timeout,
            self._route_headers(route, page_url=page_url),
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        payload = _safe_json(resp)
        if status >= 400:
            raise ProtocolPaymentError(
                f"read checkout returned HTTP {status}",
                error_code=f"checkout_read_http_{status}",
                error_stage="checkout",
                status_code=status,
            )
        if payload.get("checkout_session_id") != context["checkout_id"]:
            raise ProtocolPaymentError(
                "checkout_session_id mismatch", error_code="checkout_mismatch", error_stage="checkout"
            )
        if payload.get("publishable_key") and payload["publishable_key"] != context["publishable_key"]:
            raise ProtocolPaymentError(
                "publishable_key changed", error_code="publishable_key_mismatch", error_stage="checkout"
            )
        secret = str(payload.get("customer_session_client_secret") or "")
        if secret:
            prev = str(context.get("customer_session_client_secret") or "")
            if prev and prev != secret:
                raise ProtocolPaymentError(
                    "customer session secret changed", error_code="customer_secret_changed", error_stage="checkout"
                )
            context["customer_session_client_secret"] = secret
        return payload

    @staticmethod
    def _verified_quote(session: Mapping[str, Any], expected_currency: str) -> tuple[int, str]:
        raw_state: object = session.get("checkout_state")
        state: Mapping[str, Any] = raw_state if isinstance(raw_state, Mapping) else {}
        raw_details: object = session.get("billing_details")
        details: Mapping[str, Any] = raw_details if isinstance(raw_details, Mapping) else {}
        currencies = [
            str(v).upper() for v in (session.get("currency"), state.get("currency"), details.get("currency")) if v
        ]
        target = str(expected_currency or "").upper()
        if not currencies or any(c != target for c in currencies):
            raise ProtocolPaymentError(
                "checkout quote currency mismatch", error_code="currency_mismatch", error_stage="quote"
            )
        total = state.get("total")
        if not isinstance(total, Mapping):
            raise ProtocolPaymentError(
                "checkout quote missing total.total", error_code="quote_missing", error_stage="quote"
            )
        nested_total = total.get("total")
        if not isinstance(nested_total, Mapping):
            raise ProtocolPaymentError(
                "checkout quote missing total.total", error_code="quote_missing", error_stage="quote"
            )
        minor = nested_total.get("minorUnitsAmount")
        if not isinstance(minor, int) or isinstance(minor, bool) or minor < 0:
            raise ProtocolPaymentError(
                "checkout quote minorUnitsAmount invalid", error_code="quote_invalid", error_stage="quote"
            )
        return minor, target

    def _sync_quote(self, context: dict[str, Any], billing: Mapping[str, str]) -> None:
        page_url = self._validated_page_url(context)
        taxes_body = {
            "checkout_session_id": context["checkout_id"],
            "processor_entity": context["processor"],
            "billing_details": {
                "name": str(billing.get("name") or ""),
                "country": str(billing.get("country") or "US").upper(),
                "currency": str(context["currency"]).upper(),
                "address": {
                    "line1": str(billing.get("line1") or ""),
                    "city": str(billing.get("city") or ""),
                    "state": str(billing.get("state") or ""),
                    "postal_code": str(billing.get("postal_code") or ""),
                    "country": str(billing.get("country") or "US").upper(),
                },
            },
        }
        resp = self._checkout_post(
            CHATGPT_CHECKOUT_TAXES,
            taxes_body,
            self.access_token,
            self.cookie_header,
            self.proxy,
            self.timeout,
            self._route_headers("/backend-api/payments/checkout/taxes", page_url=page_url),
        )
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            raise ProtocolPaymentError(
                "checkout taxes sync failed", error_code="taxes_sync_failed", error_stage="quote"
            )
        payload = _safe_json(resp)
        session = payload.get("checkout_session")
        if not isinstance(session, Mapping) or session.get("checkout_session_id") != context["checkout_id"]:
            raise ProtocolPaymentError(
                "taxes response missing matching session", error_code="taxes_mismatch", error_stage="quote"
            )
        minor, currency = self._verified_quote(session, str(context["currency"]))
        context["amount_minor"] = minor
        context["currency"] = currency
        context["quoted"] = True
        tax_id_resp = self._checkout_post(
            CHATGPT_CHECKOUT_UPDATE_TAX_ID,
            {"checkout_session_id": context["checkout_id"], "tax_id_info": None},
            self.access_token,
            self.cookie_header,
            self.proxy,
            self.timeout,
            self._route_headers("/backend-api/payments/checkout/update_tax_id", page_url=page_url),
        )
        if int(getattr(tax_id_resp, "status_code", 0) or 0) >= 400:
            raise ProtocolPaymentError("update_tax_id failed", error_code="update_tax_id_failed", error_stage="quote")

    def _read_elements(self, context: dict[str, Any]) -> None:
        params: dict[str, str] = {
            "key": str(context["publishable_key"]),
            "_stripe_version": DEFAULT_STRIPE_VERSION,
            "type": "deferred_intent",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": str(context["amount_minor"]),
            "deferred_intent[currency]": str(context["currency"]).lower(),
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "link",
        }
        if context.get("customer_session_client_secret"):
            params["customer_session_client_secret"] = str(context["customer_session_client_secret"])
        resp = self.stripe_http.get(STRIPE_ELEMENTS_SESSIONS, params=params, timeout=self.timeout)
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            raise ProtocolPaymentError(
                "Stripe elements/sessions failed", error_code="elements_failed", error_stage="setup"
            )
        payload = _safe_json(resp)
        customer = _obj_id(payload.get("customer"))
        if customer.startswith("cus_"):
            prev = str(context.get("customer_id") or "")
            if prev and prev != customer:
                raise ProtocolPaymentError(
                    "Stripe customer changed", error_code="customer_mismatch", error_stage="setup"
                )
            context["customer_id"] = customer
        context["elements_read"] = True

    def _create_confirmation_token(
        self, context: dict[str, Any], card: Mapping[str, Any], billing: Mapping[str, str]
    ) -> dict[str, Any] | None:
        safe = normalize_card(card)
        context["token_started"] = True
        form: dict[str, str] = {
            "key": str(context["publishable_key"]),
            "_stripe_version": DEFAULT_STRIPE_VERSION,
            "payment_method_data[type]": "card",
            "payment_method_data[allow_redisplay]": "always",
            "setup_future_usage": "off_session",
            "set_as_default_payment_method": "false",
            "client_context[mode]": "subscription",
            "client_context[currency]": str(context["currency"]).lower(),
            "client_context[payment_method_types][0]": "card",
            "client_context[payment_method_types][1]": "link",
            "payment_method_data[card][number]": safe["number"],
            "payment_method_data[card][cvc]": safe["cvc"],
            "payment_method_data[card][exp_month]": safe["exp_month"],
            "payment_method_data[card][exp_year]": safe["exp_year"],
            "payment_method_data[billing_details][name]": str(billing.get("name") or ""),
            "payment_method_data[billing_details][email]": _billing_email(billing),
            "payment_method_data[billing_details][address][line1]": str(billing.get("line1") or ""),
            "payment_method_data[billing_details][address][city]": str(billing.get("city") or ""),
            "payment_method_data[billing_details][address][state]": str(billing.get("state") or ""),
            "payment_method_data[billing_details][address][postal_code]": str(billing.get("postal_code") or ""),
            "payment_method_data[billing_details][address][country]": str(billing.get("country") or "").upper(),
        }
        if context.get("customer_id"):
            form["client_context[customer]"] = str(context["customer_id"])
        try:
            resp = self.stripe_http.post(STRIPE_CONFIRMATION_TOKENS, data=urlencode(form), timeout=self.timeout)
        finally:
            for key in _CARD_SECRET_KEYS:
                form.pop(f"payment_method_data[card][{key}]", None)
        status = int(getattr(resp, "status_code", 0) or 0)
        payload = _safe_json(resp)
        if status >= 400:
            err = _stripe_error_fields(payload)
            msg = scrub_card_secrets(err["message"] or f"confirmation_tokens HTTP {status}", safe)
            state = "card_declined" if err["decline_code"] or status == 402 else "request_rejected"
            result = self._outcome(context, state, "setup", msg, code=err["code"], decline_code=err["decline_code"])
            context["terminal_result"] = result
            return result
        token = str(payload.get("id") or "")
        if not token.startswith("ctoken_"):
            return self._outcome(context, "unknown", "setup", "confirmation_tokens response missing ctoken_ id")
        context["confirmation_token"] = token
        preview = payload.get("payment_method_preview")
        if isinstance(preview, Mapping) and str(preview.get("id") or "").startswith("pm_"):
            context["payment_method_id"] = str(preview["id"])
        return None

    def _app_confirm(self, context: dict[str, Any]) -> dict[str, Any] | None:
        page_url = self._validated_page_url(context)
        context["app_confirmation_started"] = True
        if self._on_confirm_start is not None:
            self._on_confirm_start()
        resp = self._checkout_post(
            CHATGPT_CHECKOUT_CONFIRM,
            {
                "checkout_session_id": context["checkout_id"],
                "confirm_token": context["confirmation_token"],
                "save_payment_method_for_reuse": True,
                "selected_payment_method_type": "card",
            },
            self.access_token,
            self.cookie_header,
            self.proxy,
            self.timeout,
            self._route_headers("/backend-api/payments/checkout/confirm", page_url=page_url),
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        payload = _safe_json(resp)
        if status >= 400:
            err = _stripe_error_fields(payload)
            result = self._outcome(
                context,
                "request_rejected" if 400 <= status < 500 else "unknown",
                "payment",
                err["message"] or f"checkout/confirm HTTP {status}",
                code=err["code"],
            )
            if 400 <= status < 500:
                context["terminal_result"] = result
            return result
        if str(payload.get("status") or "").lower() in {"blocked", "failed", "error"}:
            result = self._outcome(
                context,
                "request_rejected",
                "payment",
                str(payload.get("message") or "checkout confirm blocked")[:240],
            )
            context["terminal_result"] = result
            return result
        intent_id, secret, return_url = payment_intent_from_confirm(payload)
        if not intent_id.startswith(("pi_", "seti_")) or not secret.startswith(f"{intent_id}_secret_"):
            return self._outcome(context, "unknown", "payment", "checkout/confirm missing verifiable intent")
        ret = return_url or str(context.get("confirm_return_url") or "")
        parts = urlsplit(ret)
        if parts.scheme != "https" or parts.hostname != _CHATGPT_HOST or parts.username or parts.password:
            raise ProtocolPaymentError(
                "untrusted confirm_return_url", error_code="untrusted_return_url", error_stage="payment"
            )
        context.update(intent_id=intent_id, intent_secret=secret, intent_return_url=ret)
        return None

    def _retrieve_intent(self, context: Mapping[str, Any]) -> dict[str, Any]:
        intent_id = str(context["intent_id"])
        base = STRIPE_PAYMENT_INTENTS if intent_id.startswith("pi_") else STRIPE_SETUP_INTENTS
        resp = self.stripe_http.get(
            f"{base}/{intent_id}",
            params={
                "key": str(context["publishable_key"]),
                "_stripe_version": DEFAULT_STRIPE_VERSION,
                "client_secret": str(context["intent_secret"]),
            },
            timeout=self.timeout,
        )
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            return {}
        return _safe_json(resp)

    def _subscription_active(self) -> bool:
        try:
            resp = self._checkout_get(
                CHATGPT_SUBSCRIPTIONS,
                self.access_token,
                self.cookie_header,
                self.proxy,
                self.timeout,
                {"Chatgpt-Account-Id": self.account_id} if self.account_id else None,
            )
        except (OSError, RuntimeError, ValueError):
            return False
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            return False
        payload = _safe_json(resp)
        raw_ent: object = payload.get("entitlement")
        ent: Mapping[str, Any] = raw_ent if isinstance(raw_ent, Mapping) else {}
        plan = str(payload.get("plan_type") or ent.get("subscription_plan") or "").lower()
        active = payload.get("is_active") is True or ent.get("has_active_subscription") is True
        if payload.get("is_active") is False or ent.get("has_active_subscription") is False:
            active = False
        delinquent = payload.get("is_delinquent") is True or ent.get("is_delinquent") is True
        return bool(plan) and plan != "free" and active and not delinquent

    def _poll_subscription_active(self, *, read_only: bool) -> bool:
        if self._subscription_active():
            return True
        if read_only:
            return False
        retries = max(0, min(self._max_poll_tries, 3) - 1)
        for _ in range(retries):
            self._sleep(self._poll_interval_seconds)
            if self._subscription_active():
                return True
        return False

    def _reconcile_paid_checkout(self, context: dict[str, Any], *, read_only: bool) -> dict[str, Any] | None:
        page = self._read_checkout(context)
        if page.get("status") != "complete" or page.get("payment_status") not in {"paid", "no_payment_required"}:
            return None
        if page.get("payment_status") == "no_payment_required" and int(context["amount_minor"]) != 0:
            return self._outcome(context, "unknown", "payment", "non-zero checkout returned no_payment_required")
        minor, currency = self._verified_quote(page, str(context["currency"]))
        if minor != int(context["amount_minor"]) or currency != str(context["currency"]).upper():
            return self._outcome(context, "unknown", "payment", "completed checkout quote mismatch")
        if not context.get("ack_started"):
            if read_only:
                return self._outcome(
                    context,
                    "pending",
                    "checkout",
                    "checkout paid; ack required to activate subscription",
                    intent_status="succeeded",
                    status_override="read_only",
                    ok_override=True,
                )
            context["ack_started"] = True
            page_url = self._validated_page_url(context)
            try:
                resp = self._checkout_post(
                    CHATGPT_CHECKOUT_SESSION_ACK,
                    {"processor_entity": context["processor"], "stripe_checkout_session_id": context["checkout_id"]},
                    self.access_token,
                    self.cookie_header,
                    self.proxy,
                    self.timeout,
                    self._route_headers("/backend-api/payments/checkout_session", page_url=page_url),
                )
                ack = _safe_json(resp)
                context["ack_complete"] = int(getattr(resp, "status_code", 0) or 0) < 400 and ack.get("success") is True
            except (OSError, RuntimeError, ValueError):
                context["ack_complete"] = False
        if self._poll_subscription_active(read_only=read_only):
            return self._outcome(
                context, "paid", "payment", "checkout completed and subscription active", intent_status="succeeded"
            )
        return self._outcome(
            context,
            "pending" if context.get("ack_complete") else "unknown",
            "payment",
            "payment confirmed; awaiting subscription activation",
            intent_status="succeeded",
        )

    def _submit_stripe_intent_confirm(self, context: dict[str, Any], intent_id: str) -> None:
        context["stripe_confirmation_started"] = True
        if self._on_confirm_start is not None:
            self._on_confirm_start()
        base = STRIPE_PAYMENT_INTENTS if intent_id.startswith("pi_") else STRIPE_SETUP_INTENTS
        try:
            self.stripe_http.post(
                f"{base}/{intent_id}/confirm",
                data=urlencode(
                    {
                        "key": str(context["publishable_key"]),
                        "_stripe_version": DEFAULT_STRIPE_VERSION,
                        "client_secret": str(context["intent_secret"]),
                        "confirmation_token": str(context["confirmation_token"]),
                        "return_url": str(context["intent_return_url"]),
                    }
                ),
                timeout=self.timeout,
            )
        except (OSError, RuntimeError, ValueError):
            pass

    def _classify_intent(self, context: dict[str, Any], intent: Mapping[str, Any], status: str) -> dict[str, Any]:
        if status in {"requires_payment_method", "canceled"}:
            err = _stripe_error_from_intent(intent)
            msg = (
                decline_message(err["decline_code"]) if err.get("decline_code") else err.get("message", "card declined")
            )
            return self._outcome(
                context,
                "card_declined",
                "payment",
                msg,
                intent_status=status,
                code=err.get("code", ""),
                decline_code=err.get("decline_code", ""),
            )
        if status == "requires_action":
            raw_na: object = intent.get("next_action")
            na: Mapping[str, Any] = raw_na if isinstance(raw_na, Mapping) else {}
            return self._outcome(
                context,
                "challenge_required",
                "payment",
                "Stripe requires manual verification (3DS/challenge); complete at checkout_url",
                intent_status=status,
                next_action={"type": str(na.get("type") or "requires_action")},
            )
        if status in {"succeeded", "processing", "requires_capture"}:
            return self._outcome(
                context, "pending", "payment", "payment processing; awaiting checkout settlement", intent_status=status
            )
        return self._outcome(context, "unknown", "payment", "unverified intent state", intent_status=status)

    def _reconcile_or_confirm_intent(self, context: dict[str, Any], *, read_only: bool) -> dict[str, Any]:
        paid = self._reconcile_paid_checkout(context, read_only=read_only)
        if paid:
            return paid
        intent_id = str(context.get("intent_id") or "")
        if not intent_id or not context.get("intent_secret"):
            return self._outcome(context, "unknown", "payment", "intent reference missing; refusing to retry payment")
        intent = self._retrieve_intent(context)
        if not intent or intent.get("id") != intent_id:
            return self._outcome(context, "unknown", "payment", "unable to retrieve Stripe intent")
        status = str(intent.get("status") or "")
        if intent_id.startswith("pi_"):
            raw_amt = intent.get("amount")
            if (
                not isinstance(raw_amt, int)
                or isinstance(raw_amt, bool)
                or str(intent.get("currency") or "").upper() != str(context["currency"]).upper()
                or raw_amt != int(context["amount_minor"])
            ):
                return self._outcome(context, "unknown", "payment", "Stripe intent amount/currency mismatch")
        if status == "requires_confirmation" and not context.get("stripe_confirmation_started"):
            if read_only:
                return self._outcome(
                    context,
                    "pending",
                    "checkout",
                    "intent ready; run without read_only to confirm",
                    intent_status=status,
                    status_override="read_only",
                    ok_override=True,
                )
            self._submit_stripe_intent_confirm(context, intent_id)
            for attempt in range(self._max_poll_tries):
                outcome = self._reconcile_or_confirm_intent(context, read_only=False)
                if outcome.get("state") != "pending" or attempt + 1 >= self._max_poll_tries:
                    return outcome
                self._sleep(self._poll_interval_seconds)
        return self._classify_intent(context, intent, status)

    @staticmethod
    def _populate_read_only_quote(context: dict[str, Any], original: Mapping[str, Any]) -> None:
        minor = expected_amount_minor(original)
        if minor is not None:
            context["amount_minor"] = minor
        raw_state = original.get("checkout_state")
        state = raw_state if isinstance(raw_state, Mapping) else {}
        raw_details = original.get("billing_details")
        details = raw_details if isinstance(raw_details, Mapping) else {}
        for candidate in (original.get("currency"), state.get("currency"), details.get("currency")):
            text = str(candidate or "").strip().upper()
            if text:
                context["currency"] = text
                break

    def run(
        self,
        context: dict[str, Any],
        spec: Mapping[str, Any],
        *,
        card: Mapping[str, Any] | None = None,
        billing: Mapping[str, str] | None = None,
        read_only: bool = False,
    ) -> dict[str, Any]:
        """Execute or reconcile a ChatGPT ``oaics_`` Custom Checkout session."""
        stage = "checkout"
        if isinstance(context.get("terminal_result"), Mapping):
            return dict(context["terminal_result"])
        try:
            if not context.get("checkout_id"):
                currency = str(spec.get("currency") or "USD").upper()
                amount_minor = (
                    int(spec["amount_minor"])
                    if "amount_minor" in spec and spec["amount_minor"] is not None
                    else minor_units_from_amount(spec.get("amount", Decimal("0")), currency)
                )
                checkout_url = str(spec.get("checkout_url") or "")
                context.update(
                    checkout_id=openai_internal_checkout_id(str(spec.get("checkout_id") or checkout_url)),
                    checkout_url=checkout_url,
                    publishable_key=str(spec.get("publishable_key") or ""),
                    currency=currency,
                    amount_minor=amount_minor,
                    country=str(spec.get("country") or "").strip().upper(),
                    processor=str(spec.get("processor") or processor_from_checkout_url(checkout_url) or "openai_llc"),
                    customer_session_client_secret=str(spec.get("customer_session_client_secret") or ""),
                    confirm_return_url=str(spec.get("confirm_return_url") or ""),
                )
            if not str(context.get("checkout_id") or "").startswith("oaics_"):
                return self._outcome(
                    context,
                    "integration_error",
                    "checkout",
                    "CustomCheckoutProtocolClient requires an oaics_ session",
                    code="checkout_kind_unsupported",
                )
            if context.get("app_confirmation_started"):
                return self._reconcile_or_confirm_intent(context, read_only=read_only)
            original = self._read_checkout(context)
            if original.get("status") == "complete":
                return self._reconcile_paid_checkout(context, read_only=read_only) or self._outcome(
                    context, "unknown", "payment", "checkout complete; payment state unverified"
                )
            if original.get("status") != "open" or original.get("payment_status") != "unpaid":
                return self._outcome(context, "request_rejected", "checkout", "checkout is not open and unpaid")
            if context.get("token_started") and not context.get("confirmation_token"):
                return self._outcome(
                    context, "unknown", "setup", "confirmation token creation unverified; refusing duplicate submission"
                )
            if read_only:
                self._populate_read_only_quote(context, original)
                return self._outcome(
                    context,
                    "read_only",
                    "checkout",
                    "checkout open and unpaid (read-only inspection)",
                    status_override="read_only",
                    ok_override=True,
                )
            target_country = (
                str((billing or {}).get("country") or spec.get("country") or context.get("country") or "US")
                .strip()
                .upper()
                or "US"
            )
            resolved_billing = _resolve_billing(billing, country=target_country)
            self._sync_quote(context, resolved_billing)
            expected_currency = str(spec.get("expected_currency") or context["currency"]).upper()
            expected_minor = (
                int(spec["expected_amount_minor"])
                if "expected_amount_minor" in spec and spec["expected_amount_minor"] is not None
                else (
                    minor_units_from_amount(spec["expected_amount"], expected_currency)
                    if "expected_amount" in spec and spec["expected_amount"] is not None
                    else int(context["amount_minor"])
                )
            )
            if int(context["amount_minor"]) != expected_minor or str(context["currency"]).upper() != expected_currency:
                return self._outcome(
                    context, "pending", "checkout", "taxes changed payable amount; confirm updated quote before paying"
                )
            if not context.get("confirmation_token"):
                stage = "setup"
                if card is None:
                    raise ProtocolPaymentError(
                        "card payload required for custom checkout",
                        error_code="missing_card_payload",
                        error_stage="setup",
                    )
                self._read_elements(context)
                stopped = self._create_confirmation_token(context, card, resolved_billing)
                if stopped:
                    return stopped
            stage = "payment"
            stopped = self._app_confirm(context)
            return stopped or self._reconcile_or_confirm_intent(context, read_only=False)
        except ProtocolPaymentError as exc:
            return self._outcome(context, "unknown", exc.error_stage or stage, str(exc)[:240], code=exc.error_code)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            return self._outcome(
                context, "unknown", stage, f"upstream failure ({type(exc).__name__}); verify before retrying"
            )
