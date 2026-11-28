"""Non-CAPTCHA Stripe Hosted Checkout (cs_live_... / cs_test_...) protocol client."""

from __future__ import annotations

import html
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from .checkout_contract import (
    OPENAI_PAY_BASE,
    STRIPE_API_BASE,
    STRIPE_ELEMENTS_SESSIONS,
    STRIPE_PAYMENT_METHODS,
    STRIPE_PAYMENT_PAGES,
    openai_pay_page,
)
from .protocol_codec import (
    billing_currency_for,
    billing_locale_for,
    billing_timezone_for,
    classify_stripe_outcome,
    decline_message,
    expected_amount_minor,
    normalize_card,
    payment_page_settled,
    rv_timestamp_from_script,
    scrub_card_secrets,
    stripe_checkout_id,
    submission_failure_detail,
)
from .protocol_pay_common import (
    HOSTED_API_VERSION,
    HOSTED_ELEMENTS_VERSION,
    HOSTED_PAYMENT_UA,
    ProtocolPaymentError,
    _CARD_SECRET_KEYS,
    _CS_ID_FULL_RE,
    _MAX_HTML_BYTES,
    _MAX_JS_BYTES,
    _OPENAI_PAY_HOST,
    _SCRIPT_REF_RE,
    _STRIPE_JS_HOST,
    _billing_email,
    _hosted_stage,
    _local_device_ids,
    _resolve_billing,
    _safe_json,
    _stripe_error_fields,
)
from .wire import _compact_diagnostic, _new_session


class HostedCheckoutProtocolClient:
    """Non-CAPTCHA Stripe Hosted Checkout (``cs_live_...`` / ``cs_test_...``) protocol client."""

    def __init__(
        self,
        *,
        proxy: str = "",
        timeout: int = 30,
        session: Any | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.proxy = str(proxy or "").strip()
        self.timeout = max(5, int(timeout or 30))
        self.http = session if session is not None else _new_session(self.proxy)
        self._sleep = sleep_fn or time.sleep
        headers: Any = getattr(self.http, "headers", None)
        if headers is not None and hasattr(headers, "update"):
            headers.update({"Origin": OPENAI_PAY_BASE, "Referer": f"{OPENAI_PAY_BASE}/"})

    def _stripe_url(self, path: str) -> str:
        clean = str(path or "").lstrip("/")
        if clean.startswith("payment_pages/"):
            return f"{STRIPE_PAYMENT_PAGES}/{clean.removeprefix('payment_pages/')}"
        if clean == "elements/sessions":
            return STRIPE_ELEMENTS_SESSIONS
        return f"{STRIPE_API_BASE}/v1/{clean}"

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        stage = _hosted_stage(path)
        submitted = method == "POST" and path.endswith("/confirm")
        attempts = 1 if submitted else 3
        url = self._stripe_url(path)
        caller = getattr(self.http, method.lower())
        for attempt in range(attempts):
            try:
                response = caller(url, timeout=self.timeout, **kwargs)
            except (OSError, RuntimeError, ValueError) as exc:
                if not submitted and attempt + 1 < attempts:
                    self._sleep(0.6 * (attempt + 1))
                    continue
                if submitted:
                    raise ProtocolPaymentError(
                        "Stripe confirm transport interrupted; outcome unknown — poll status instead of retrying",
                        error_code="payment_outcome_unknown",
                        error_stage="confirm",
                        retryable=False,
                    ) from exc
                raise ProtocolPaymentError(
                    f"Stripe {stage} transport failed ({type(exc).__name__})",
                    error_code="stripe_transport_error",
                    error_stage=stage,
                    retryable=True,
                ) from exc
            return self._parse_response(response, stage=stage)
        raise ProtocolPaymentError("Stripe request loop exhausted", error_stage=stage)

    @staticmethod
    def _parse_response(response: Any, *, stage: str) -> dict[str, Any]:
        status = int(getattr(response, "status_code", 0) or 0)
        payload = _safe_json(response)
        if status >= 400:
            err = _stripe_error_fields(payload)
            detail = err["message"] or _compact_diagnostic(getattr(response, "text", "") or "")
            reason_suffix = f" [reason={err['reason']}]" if err["reason"] else ""
            code = err["code"] or f"stripe_http_{status}"
            raise ProtocolPaymentError(
                f"Stripe {stage} HTTP {status} ({code}): {detail}{reason_suffix}".strip(),
                error_code=code,
                error_stage=stage,
                retryable=status in {408, 429} or 500 <= status <= 599,
                status_code=status,
                stripe_code=err["code"],
                decline_code=err["decline_code"],
                confirm_error_reason=err["reason"],
            )
        return payload

    def fetch_live_rv_timestamp(self, session_id: str) -> str:
        """Read live Hosted page + Stripe ``checkout-app-init`` bundle and compute ``rv_timestamp``."""
        if not _CS_ID_FULL_RE.fullmatch(str(session_id or "")):
            return ""
        page_url = openai_pay_page(session_id)
        fetch_timeout = min(self.timeout, 15)
        try:
            page = self.http.get(page_url, timeout=fetch_timeout, allow_redirects=False)
            if int(getattr(page, "status_code", 0) or 0) != 200:
                return ""
            text = str(getattr(page, "text", "") or "")
            if len(text) > _MAX_HTML_BYTES:
                return ""
            direct = rv_timestamp_from_script(text)
            if direct:
                return direct
            for raw in _SCRIPT_REF_RE.findall(text):
                script_url = html.unescape(urljoin(page_url, raw))
                parsed = urlsplit(script_url)
                if parsed.scheme != "https" or parsed.hostname != _STRIPE_JS_HOST:
                    continue
                if parsed.username or parsed.password:
                    continue
                if (
                    not parsed.path.startswith("/v3/")
                    or "checkout-app-init" not in parsed.path
                    or not parsed.path.endswith(".js")
                ):
                    continue
                script = self.http.get(script_url, timeout=fetch_timeout, allow_redirects=False)
                if int(getattr(script, "status_code", 0) or 0) != 200:
                    return ""
                source = str(getattr(script, "text", "") or "")
                return (rv_timestamp_from_script(source) or "") if len(source) <= _MAX_JS_BYTES else ""
        except (OSError, RuntimeError, ValueError):
            return ""
        return ""

    def quote(
        self,
        *,
        session_id: str,
        publishable_key: str,
        billing: Mapping[str, str] | None = None,
        currency: str = "USD",
        browser_locale: str = "",
        browser_timezone: str = "",
    ) -> dict[str, Any]:
        """Run Hosted Checkout ``/init`` and optional billing/tax synchronization."""
        cs_id = stripe_checkout_id(session_id)
        key = str(publishable_key or "").strip()
        if not cs_id or not key.startswith("pk_"):
            raise ProtocolPaymentError(
                "Hosted Checkout requires a valid cs_ session id and pk_ publishable key",
                error_code="hosted_session_invalid",
                error_stage="validation",
            )
        country = str((billing or {}).get("country") or "US").strip().upper() or "US"
        locale = browser_locale or billing_locale_for(country)
        timezone = browser_timezone or billing_timezone_for(country)
        prefix = f"payment_pages/{cs_id}"
        init = self._request(
            "POST",
            f"{prefix}/init",
            data=urlencode(
                {
                    "key": key,
                    "eid": "NA",
                    "redirect_type": "url",
                    "browser_locale": locale,
                    "browser_timezone": timezone,
                }
            ),
        )
        eid = str(init.get("eid") or "NA")
        tax = init
        if isinstance(billing, Mapping) and billing.get("country"):
            self._request("POST", prefix, data=urlencode({"key": key, "eid": eid, "tax_region[country]": country}))
            self._request(
                "POST",
                prefix,
                data=urlencode({"key": key, "eid": eid, "tax_id_collection[purchasing_as_business]": "false"}),
            )
            address: dict[str, str] = {"key": key, "eid": eid}
            for field in ("country", "state", "postal_code", "line1", "city"):
                val = str(billing.get(field) or "").strip()
                if val:
                    address[f"tax_region[{field}]"] = val
            tax = self._request("POST", prefix, data=urlencode(address))
        amount = expected_amount_minor(tax)
        if amount is None:
            amount = expected_amount_minor(init)
        if amount is None:
            raise ProtocolPaymentError(
                "Hosted Checkout response did not include a payable minor-unit amount",
                error_code="hosted_amount_missing",
                error_stage="quote",
            )
        resolved_currency = str(init.get("currency") or currency or billing_currency_for(country)).lower()
        return {
            "ok": True,
            "session_id": cs_id,
            "checkout_url": openai_pay_page(cs_id),
            "publishable_key": key,
            "eid": eid,
            "amount_minor": amount,
            "currency": resolved_currency,
            "billing": dict(billing or {}),
            "checkout_config_id": str(tax.get("config_id") or init.get("config_id") or ""),
            "customer_email": str((billing or {}).get("email") or init.get("customer_email") or ""),
        }

    def prepare(
        self,
        *,
        session_id: str,
        publishable_key: str,
        billing: Mapping[str, str],
        currency: str = "USD",
        browser_locale: str = "",
        browser_timezone: str = "",
        fetch_timing: bool = True,
    ) -> dict[str, Any]:
        """Prepare Hosted Checkout context: quote, elements session, config refresh, rv_timestamp."""
        country = str(billing.get("country") or "US").strip().upper() or "US"
        locale = browser_locale or billing_locale_for(country)
        timezone = browser_timezone or billing_timezone_for(country)
        quoted = self.quote(
            session_id=session_id,
            publishable_key=publishable_key,
            billing=billing,
            currency=currency,
            browser_locale=locale,
            browser_timezone=timezone,
        )
        cs_id = quoted["session_id"]
        amount = quoted["amount_minor"]
        code = quoted["currency"]
        eid = quoted["eid"]
        js_id = str(uuid.uuid4())
        elements = self._request(
            "GET",
            "elements/sessions",
            params={
                "key": publishable_key,
                "type": "deferred_intent",
                "checkout_session_id": cs_id,
                "hosted_surface": "checkout",
                "elements_init_source": "checkout",
                "referrer_host": _OPENAI_PAY_HOST,
                "stripe_js_id": js_id,
                "locale": locale,
                "browser_timezone": timezone,
                "deferred_intent[mode]": "subscription",
                "deferred_intent[amount]": str(amount),
                "deferred_intent[currency]": code,
                "deferred_intent[setup_future_usage]": "off_session",
                "deferred_intent[payment_method_types][0]": "card",
                "client_betas[0]": "google_pay_beta_1",
                "client_betas[1]": "disable_deferred_intent_client_validation_beta_1",
                "client_betas[2]": "blocked_card_brands_beta_2",
                "_stripe_version": HOSTED_ELEMENTS_VERSION,
            },
        )
        if billing.get("country"):
            refresh: dict[str, str] = {"key": publishable_key, "eid": eid}
            for field in ("country", "state", "postal_code", "line1", "city"):
                val = str(billing.get(field) or "").strip()
                if val:
                    refresh[f"tax_region[{field}]"] = val
            refreshed = self._request("POST", f"payment_pages/{cs_id}", data=urlencode(refresh))
            latest_cfg = str(refreshed.get("config_id") or "")
            if latest_cfg:
                quoted["checkout_config_id"] = latest_cfg
            refreshed_amount = expected_amount_minor(refreshed)
            if refreshed_amount is not None:
                amount = refreshed_amount
        timing = self.fetch_live_rv_timestamp(cs_id) if fetch_timing else ""
        device = _local_device_ids(js_id)
        return {
            "context_id": str(uuid.uuid4()),
            "session_id": cs_id,
            "checkout_url": openai_pay_page(cs_id),
            "publishable_key": publishable_key,
            "eid": eid,
            "amount_minor": amount,
            "currency": code,
            "checkout_config_id": quoted["checkout_config_id"],
            "elements_session_id": str(elements.get("session_id") or ""),
            "client_session_id": device["client_session_id"],
            "guid": device["guid"],
            "muid": device["muid"],
            "sid": device["sid"],
            "billing": dict(billing),
            "customer_email": quoted["customer_email"],
            "rv_timestamp": timing,
        }

    def tokenize_card(
        self,
        *,
        card: Mapping[str, Any],
        billing: Mapping[str, str],
        publishable_key: str,
        session_id: str,
        checkout_config_id: str = "",
        device: Mapping[str, str] | None = None,
    ) -> str:
        """Tokenize a card on Stripe's ``/v1/payment_methods`` surface and return only ``pm_...``."""
        key = str(publishable_key or "").strip()
        cs_id = stripe_checkout_id(session_id)
        if not key.startswith("pk_") or not cs_id:
            raise ProtocolPaymentError(
                "missing Stripe publishable key or cs_ session id for card tokenization",
                error_code="tokenize_invalid_context",
                error_stage="tokenize",
            )
        try:
            safe = normalize_card(card)
        except ValueError as exc:
            raise ProtocolPaymentError(
                str(exc),
                error_code="invalid_card_fields",
                error_stage="tokenize",
            ) from exc
        dev = dict(device or {})
        form: dict[str, str] = {
            "type": "card",
            "card[number]": safe["number"],
            "card[cvc]": safe["cvc"],
            "card[exp_month]": safe["exp_month"],
            "card[exp_year]": safe["exp_year"],
            "billing_details[name]": str(billing.get("name") or ""),
            "billing_details[email]": _billing_email(billing),
            "billing_details[address][country]": str(billing.get("country") or "").upper(),
            "billing_details[address][line1]": str(billing.get("line1") or ""),
            "billing_details[address][city]": str(billing.get("city") or ""),
            "billing_details[address][state]": str(billing.get("state") or ""),
            "billing_details[address][postal_code]": str(billing.get("postal_code") or ""),
            "payment_user_agent": HOSTED_PAYMENT_UA,
            "client_attribution_metadata[client_session_id]": str(dev.get("client_session_id") or uuid.uuid4()),
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[checkout_config_id]": str(checkout_config_id or ""),
            "guid": str(dev.get("guid") or ""),
            "muid": str(dev.get("muid") or ""),
            "sid": str(dev.get("sid") or ""),
            "key": key,
            "_stripe_version": HOSTED_API_VERSION,
        }
        headers = {
            "Origin": OPENAI_PAY_BASE,
            "Referer": f"{OPENAI_PAY_BASE}/",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        response: Any = None
        last_exc: Exception | None = None
        try:
            for attempt in range(3):
                try:
                    response = self.http.post(STRIPE_PAYMENT_METHODS, data=form, headers=headers, timeout=self.timeout)
                    break
                except (OSError, RuntimeError, ValueError) as exc:
                    last_exc = exc
                    if attempt < 2:
                        self._sleep(0.6 * (attempt + 1))
        finally:
            for secret_key in _CARD_SECRET_KEYS:
                form.pop(f"card[{secret_key}]", None)
        if response is None:
            raise ProtocolPaymentError(
                "Stripe payment_methods transport interrupted before response; safe to retry",
                error_code="tokenize_transport_error",
                error_stage="tokenize",
                retryable=True,
            ) from last_exc
        status = int(getattr(response, "status_code", 0) or 0)
        payload = _safe_json(response)
        if status == 200:
            pm_id = str(payload.get("id") or "")
            if pm_id.startswith("pm_"):
                return pm_id
        err = _stripe_error_fields(payload)
        scrubbed = scrub_card_secrets(err["message"] or f"Stripe payment_methods failed ({status})", safe)
        raise ProtocolPaymentError(
            scrubbed,
            error_code=err["code"] or f"tokenize_http_{status}",
            error_stage="tokenize",
            retryable=False,
            status_code=status,
            stripe_code=err["code"],
            decline_code=err["decline_code"],
        )

    def confirm(self, context: Mapping[str, Any], payment_method: str, *, rv_timestamp: str = "") -> dict[str, Any]:
        """Submit a single non-retried Hosted Checkout ``/confirm`` call."""
        pm = str(payment_method or "").strip()
        if not pm.startswith("pm_"):
            raise ProtocolPaymentError(
                "Hosted Checkout confirm requires a pm_ payment method id",
                error_code="invalid_payment_method",
                error_stage="confirm",
            )
        body: dict[str, str] = {
            "key": str(context["publishable_key"]),
            "_stripe_version": HOSTED_API_VERSION,
            "eid": str(context["eid"]),
            "payment_method": pm,
            "expected_amount": str(context["amount_minor"]),
            "consent[terms_of_service]": "accepted",
            "expected_payment_method_type": "card",
            "guid": str(context["guid"]),
            "muid": str(context["muid"]),
            "sid": str(context["sid"]),
            "client_attribution_metadata[client_session_id]": str(context["client_session_id"]),
            "client_attribution_metadata[checkout_session_id]": str(context["session_id"]),
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_version]": "hosted_checkout",
            "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
            "client_attribution_metadata[checkout_config_id]": str(context.get("checkout_config_id") or ""),
            "link_brand": "link",
            "passive_captcha_ekey": "",
        }
        timing = str(context.get("rv_timestamp") or rv_timestamp or "")
        if timing:
            body["rv_timestamp"] = timing
        return self._request("POST", f"payment_pages/{context['session_id']}/confirm", data=urlencode(body))

    def poll(self, *, session_id: str, publishable_key: str) -> dict[str, Any]:
        """Read-only ``GET /v1/payment_pages/{cs_id}/poll``."""
        cs_id = stripe_checkout_id(session_id)
        return self._request("GET", f"payment_pages/{cs_id}/poll", params={"key": publishable_key})

    @staticmethod
    def _outcome_from_confirm_error(exc: ProtocolPaymentError) -> dict[str, Any]:
        return classify_stripe_outcome(
            {
                "status": exc.status_code,
                "body": {
                    "error": {
                        "code": exc.stripe_code or exc.error_code,
                        "decline_code": exc.decline_code,
                        "message": str(exc),
                    }
                },
            }
        )

    def _confirm_with_mismatch_retry(
        self,
        ctx: dict[str, Any],
        pm_id: str,
        *,
        cs_id: str,
        publishable_key: str,
        resolved_billing: Mapping[str, str],
        resolved_currency: str,
        on_confirm_start: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if on_confirm_start is not None:
            on_confirm_start()
        try:
            return self.confirm(ctx, pm_id), None
        except ProtocolPaymentError as exc:
            if exc.stripe_code != "checkout_amount_mismatch":
                return {}, self._outcome_from_confirm_error(exc)
        refreshed = self.quote(
            session_id=cs_id,
            publishable_key=publishable_key,
            billing=resolved_billing,
            currency=resolved_currency,
        )
        ctx["amount_minor"] = refreshed["amount_minor"]
        if refreshed.get("eid"):
            ctx["eid"] = refreshed["eid"]
        if refreshed.get("checkout_config_id"):
            ctx["checkout_config_id"] = refreshed["checkout_config_id"]
        try:
            return self.confirm(ctx, pm_id), None
        except ProtocolPaymentError as retry_exc:
            return {}, self._outcome_from_confirm_error(retry_exc)

    def pay(
        self,
        checkout_url: str,
        *,
        publishable_key: str,
        card: Mapping[str, Any] | None = None,
        payment_method_id: str = "",
        billing: Mapping[str, str] | None = None,
        country: str = "US",
        currency: str = "",
        max_poll_tries: int = 10,
        poll_interval_seconds: float = 1.0,
        fetch_timing: bool = True,
        on_confirm_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Run the full non-CAPTCHA Hosted Checkout card payment flow."""
        cs_id = stripe_checkout_id(checkout_url)
        if not cs_id:
            raise ProtocolPaymentError(
                "checkout_url does not contain a Stripe cs_ session id",
                error_code="hosted_session_invalid",
                error_stage="validation",
            )
        resolved_billing = _resolve_billing(billing, country=country)
        resolved_currency = str(currency or billing_currency_for(resolved_billing["country"])).upper()
        ctx = self.prepare(
            session_id=cs_id,
            publishable_key=publishable_key,
            billing=resolved_billing,
            currency=resolved_currency,
            fetch_timing=fetch_timing,
        )
        pm_id = str(payment_method_id or "").strip()
        if not pm_id:
            if card is None:
                raise ProtocolPaymentError(
                    "either card or payment_method_id is required for hosted payment",
                    error_code="missing_payment_method",
                    error_stage="validation",
                )
            pm_id = self.tokenize_card(
                card=card,
                billing=resolved_billing,
                publishable_key=publishable_key,
                session_id=cs_id,
                checkout_config_id=ctx["checkout_config_id"],
                device=ctx,
            )

        confirm_payload, error_outcome = self._confirm_with_mismatch_retry(
            ctx,
            pm_id,
            cs_id=cs_id,
            publishable_key=publishable_key,
            resolved_billing=resolved_billing,
            resolved_currency=resolved_currency,
            on_confirm_start=on_confirm_start,
        )
        if error_outcome is not None:
            return self._format_hosted_result(ctx, pm_id, error_outcome, confirm_payload={})

        initial = classify_stripe_outcome({"status": 200, "body": confirm_payload})
        poll_payload: dict[str, Any] | None = None
        if initial["state"] not in {"paid", "card_declined", "confirm_error", "challenge_required", "unknown"}:
            if not payment_page_settled(confirm_payload):
                for _ in range(max(1, int(max_poll_tries))):
                    self._sleep(max(0.0, float(poll_interval_seconds)))
                    poll_payload = self.poll(session_id=cs_id, publishable_key=publishable_key)
                    if payment_page_settled(poll_payload):
                        break
        outcome = classify_stripe_outcome({"status": 200, "body": confirm_payload}, poll_payload)
        return self._format_hosted_result(
            ctx, pm_id, outcome, confirm_payload=confirm_payload, poll_payload=poll_payload
        )

    @staticmethod
    def _format_hosted_result(
        ctx: Mapping[str, Any],
        pm_id: str,
        outcome: Mapping[str, Any],
        *,
        confirm_payload: Mapping[str, Any],
        poll_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = str(outcome.get("state") or "unknown")
        decline_code = str(outcome.get("decline_code") or "")
        message = str(outcome.get("message") or "")
        if state == "card_declined" and decline_code:
            message = decline_message(decline_code)
        elif state == "challenge_required" and not message:
            message = "Stripe requires manual verification (3DS/challenge); complete it directly at checkout_url"
        elif state == "unknown" and not message:
            message = "Stripe confirm outcome unknown; reconcile checkout status before retrying"
        failure_detail = submission_failure_detail(poll_payload or confirm_payload)
        status = "completed" if state == "paid" else ("unknown" if state == "unknown" else "failed")
        result: dict[str, Any] = {
            "ok": state == "paid",
            "status": status,
            "mode": "hosted_checkout",
            "state": state,
            "session_id": str(ctx["session_id"]),
            "checkout_url": str(ctx["checkout_url"]),
            "amount_minor": int(ctx["amount_minor"]),
            "currency": str(ctx["currency"]).upper(),
            "payment_method_id": str(outcome.get("payment_method_id") or pm_id),
            "intent_status": str(outcome.get("intent_status") or ""),
            "charge_id": str(outcome.get("charge_id") or ""),
            "code": str(outcome.get("code") or ""),
            "decline_code": decline_code,
            "challenge_type": str(outcome.get("challenge_type") or ""),
            "manual_action_required": state == "challenge_required",
            "message": message,
            "failure_detail": failure_detail,
            "rv_timestamp_present": bool(ctx.get("rv_timestamp")),
        }
        if state == "unknown":
            result["requires_reconciliation"] = True
            result["side_effect_started"] = True
            result["retryable"] = False
        elif state == "pending":
            result["side_effect_started"] = True
            result["retryable"] = False
        return result
