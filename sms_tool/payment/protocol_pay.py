"""Non-CAPTCHA Stripe Hosted & ChatGPT Custom Checkout HTTP protocol clients.

Migrated from TeamGo's ``hosted.py``, ``tokenize.py``, and ``checkout_protocol.py``
with strict safety and financial idempotency boundaries:

* Zero CAPTCHA / anti-bot solving: whenever Stripe responds with
  ``requires_action`` (3DS or ``intent_confirmation_challenge``), execution stops
  immediately and returns ``state="challenge_required"`` alongside the checkout
  URL so an operator can finish verification manually.
* Single-shot financial confirmation: ``/confirm`` never retries automatically
  after a lost response (preventing duplicate authorizations).
* Card hygiene: card fields are loaded only from ``--card-file`` or
  ``GPT_CARD_JSON`` (never CLI argv), scrubbed from every error message, and
  dropped from local dicts immediately after tokenization.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from .checkout_contract import (
    CHATGPT_BASE,
    CHATGPT_CHECKOUT_API,
    CHATGPT_CHECKOUT_CONFIRM,
    CHATGPT_CHECKOUT_SESSION_ACK,
    CHATGPT_CHECKOUT_TAXES,
    CHATGPT_CHECKOUT_UPDATE_TAX_ID,
    CHATGPT_SUBSCRIPTIONS,
    DEFAULT_STRIPE_VERSION,
    OPENAI_PAY_BASE,
    STRIPE_API_BASE,
    STRIPE_CONFIRMATION_TOKENS,
    STRIPE_ELEMENTS_SESSIONS,
    STRIPE_JS_ORIGIN,
    STRIPE_PAYMENT_INTENTS,
    STRIPE_PAYMENT_METHODS,
    STRIPE_PAYMENT_PAGES,
    STRIPE_SETUP_INTENTS,
    openai_pay_page,
)
from .protocol_codec import (
    billing_address_for,
    billing_currency_for,
    billing_locale_for,
    billing_timezone_for,
    classify_stripe_outcome,
    decline_message,
    expected_amount_minor,
    minor_units_from_amount,
    normalize_card,
    openai_internal_checkout_id,
    payment_intent_from_confirm,
    payment_page_settled,
    processor_from_checkout_url,
    rv_timestamp_from_script,
    scrub_card_secrets,
    stripe_checkout_id,
    submission_failure_detail,
)
from .wire import _checkout_get, _checkout_post, _compact_diagnostic, _new_session

HOSTED_API_VERSION = "2020-08-27;custom_checkout_beta=v1"
HOSTED_ELEMENTS_VERSION = "2026-03-25.dahlia"
HOSTED_PAYMENT_UA = "stripe.js/2bae21f786; stripe-js-v3/2bae21f786; checkout"

_OPENAI_PAY_HOST = urlsplit(OPENAI_PAY_BASE).hostname or ""
_STRIPE_JS_HOST = urlsplit(STRIPE_JS_ORIGIN).hostname or ""
_CHATGPT_HOST = urlsplit(CHATGPT_BASE).hostname or ""
_CARD_SECRET_KEYS = ("number", "cvc", "exp_month", "exp_year")
_CS_ID_FULL_RE = re.compile(r"cs_(?:live|test)_[A-Za-z0-9_]+")
_SCRIPT_REF_RE = re.compile(r'(?:src|href)=["\']([^"\']+)["\']')
_MAX_HTML_BYTES = 4_000_000
_MAX_JS_BYTES = 8_000_000


class ProtocolPaymentError(RuntimeError):
    """Structured error raised by Hosted or Custom Checkout protocol operations."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "protocol_payment_error",
        error_stage: str = "protocol_payment",
        retryable: bool = False,
        status_code: int = 0,
        stripe_code: str = "",
        decline_code: str = "",
        confirm_error_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code or "protocol_payment_error")
        self.error_stage = str(error_stage or "protocol_payment")
        self.retryable = bool(retryable)
        self.status_code = int(status_code or 0)
        self.stripe_code = str(stripe_code or "")
        self.decline_code = str(decline_code or "")
        self.confirm_error_reason = str(confirm_error_reason or "")


def _new_device_id() -> str:
    return str(uuid.uuid4()) + uuid.uuid4().hex[:6]


def _local_device_ids(client_session_id: str = "") -> dict[str, str]:
    return {
        "guid": _new_device_id(),
        "muid": _new_device_id(),
        "sid": _new_device_id(),
        "client_session_id": client_session_id or str(uuid.uuid4()),
    }


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        parsed = response.json()
    except (ValueError, TypeError, AttributeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _stripe_error_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    raw_err = payload.get("error")
    err = raw_err if isinstance(raw_err, Mapping) else {}
    raw_extra = err.get("extra_fields")
    extra = raw_extra if isinstance(raw_extra, Mapping) else {}
    return {
        "type": str(err.get("type") or ""),
        "code": str(err.get("code") or ""),
        "decline_code": str(err.get("decline_code") or ""),
        "param": str(err.get("param") or ""),
        "reason": str(extra.get("confirm_error_reason") or ""),
        "message": str(err.get("message") or "")[:300],
    }


def _stripe_error_from_intent(payload: Mapping[str, Any]) -> dict[str, str]:
    err = payload.get("last_payment_error") or payload.get("last_setup_error") or payload.get("error")
    if not isinstance(err, Mapping):
        return {}
    out: dict[str, str] = {}
    for key in ("code", "decline_code", "type", "message"):
        val = str(err.get(key) or "").strip()
        if val:
            out[key] = val[:240]
    return out


def _obj_id(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return str(value.get("id") or "").strip()
    return ""


def _hosted_stage(path: str) -> str:
    if path.endswith("/init"):
        return "init"
    if path.endswith("/confirm"):
        return "confirm"
    if path.endswith("/poll"):
        return "poll"
    if path == "elements/sessions":
        return "elements"
    return "tax_sync"


def _billing_email(billing: Mapping[str, str]) -> str:
    email = str(billing.get("email") or "").strip()
    if email:
        return email
    slug = re.sub(r"[^a-z0-9]+", ".", str(billing.get("name") or "cardholder").lower()).strip(".") or "cardholder"
    return f"{slug}.{uuid.uuid4().hex[:6]}@example.com"


def _resolve_billing(billing: Mapping[str, str] | None, *, country: str = "") -> dict[str, str]:
    target_country = str((billing or {}).get("country") or country or "US").strip().upper() or "US"
    base = billing_address_for(target_country)
    if isinstance(billing, Mapping):
        for key, value in billing.items():
            text = str(value or "").strip()
            if text:
                base[str(key)] = text
    base["country"] = str(base.get("country") or target_country).upper()
    return base


def _custom_outcome(
    state: str,
    stage: str,
    message: str,
    *,
    intent_status: str = "",
    code: str = "",
    decline_code: str = "",
    next_action: dict[str, Any] | None = None,
    checkout_url: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": state == "paid",
        "status": "completed" if state == "paid" else "failed",
        "state": state,
        "stage": stage,
        "message": message,
        "intent_status": intent_status,
        "code": code,
        "decline_code": decline_code,
        "manual_action_required": state == "challenge_required",
    }
    if checkout_url:
        result["checkout_url"] = checkout_url
    if next_action:
        result["next_action"] = next_action
    return result


def load_card_from_env_or_file(card_file: str = "") -> dict[str, str]:
    """Load and validate card JSON from ``card_file`` or ``GPT_CARD_JSON`` env var."""
    path_str = str(card_file or "").strip()
    raw_text = ""
    source = "GPT_CARD_JSON"
    if path_str:
        source = "card_file"
        try:
            raw_text = Path(path_str).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ProtocolPaymentError(
                f"failed to read card file: {exc}",
                error_code="card_file_unreadable",
                error_stage="validation",
            ) from exc
    else:
        raw_text = str(os.environ.get("GPT_CARD_JSON") or "").strip()
    if not raw_text:
        raise ProtocolPaymentError(
            "card data required via --card-file or GPT_CARD_JSON (or pass --payment-method-id)",
            error_code="missing_card_payload",
            error_stage="validation",
        )
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise ProtocolPaymentError(
            f"invalid JSON in {source}",
            error_code="invalid_card_json",
            error_stage="validation",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProtocolPaymentError(
            f"card payload in {source} must be a JSON object",
            error_code="invalid_card_payload",
            error_stage="validation",
        )
    try:
        return normalize_card(parsed)
    except ValueError as exc:
        raise ProtocolPaymentError(
            str(exc),
            error_code="invalid_card_fields",
            error_stage="validation",
        ) from exc


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

        confirm_payload: dict[str, Any] = {}
        try:
            confirm_payload = self.confirm(ctx, pm_id)
        except ProtocolPaymentError as exc:
            if exc.stripe_code == "checkout_amount_mismatch":
                refreshed = self.quote(
                    session_id=cs_id,
                    publishable_key=publishable_key,
                    billing=resolved_billing,
                    currency=resolved_currency,
                )
                ctx["amount_minor"] = refreshed["amount_minor"]
                if refreshed.get("checkout_config_id"):
                    ctx["checkout_config_id"] = refreshed["checkout_config_id"]
                confirm_payload = self.confirm(ctx, pm_id)
            else:
                outcome = classify_stripe_outcome(
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
                return self._format_hosted_result(ctx, pm_id, outcome, confirm_payload={})

        initial = classify_stripe_outcome({"status": 200, "body": confirm_payload})
        poll_payload: dict[str, Any] | None = None
        if initial["state"] not in {"paid", "card_declined", "confirm_error", "challenge_required"}:
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
        failure_detail = submission_failure_detail(poll_payload or confirm_payload)
        return {
            "ok": state == "paid",
            "status": "completed" if state == "paid" else "failed",
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
            result = _custom_outcome(state, "setup", msg, code=err["code"], decline_code=err["decline_code"])
            context["terminal_result"] = result
            return result
        token = str(payload.get("id") or "")
        if not token.startswith("ctoken_"):
            return _custom_outcome("unknown", "setup", "confirmation_tokens response missing ctoken_ id")
        context["confirmation_token"] = token
        preview = payload.get("payment_method_preview")
        if isinstance(preview, Mapping) and str(preview.get("id") or "").startswith("pm_"):
            context["payment_method_id"] = str(preview["id"])
        return None

    def _app_confirm(self, context: dict[str, Any]) -> dict[str, Any] | None:
        page_url = self._validated_page_url(context)
        context["app_confirmation_started"] = True
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
            result = _custom_outcome(
                "request_rejected" if 400 <= status < 500 else "unknown",
                "payment",
                err["message"] or f"checkout/confirm HTTP {status}",
                code=err["code"],
            )
            if 400 <= status < 500:
                context["terminal_result"] = result
            return result
        if str(payload.get("status") or "").lower() in {"blocked", "failed", "error"}:
            result = _custom_outcome(
                "request_rejected", "payment", str(payload.get("message") or "checkout confirm blocked")[:240]
            )
            context["terminal_result"] = result
            return result
        intent_id, secret, return_url = payment_intent_from_confirm(payload)
        if not intent_id.startswith(("pi_", "seti_")) or not secret.startswith(f"{intent_id}_secret_"):
            return _custom_outcome("unknown", "payment", "checkout/confirm missing verifiable intent")
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
        return plan in {"plus", "chatgptplusplan"} and active and not delinquent

    def _reconcile_paid_checkout(self, context: dict[str, Any], *, read_only: bool) -> dict[str, Any] | None:
        page = self._read_checkout(context)
        if page.get("status") != "complete" or page.get("payment_status") not in {"paid", "no_payment_required"}:
            return None
        if page.get("payment_status") == "no_payment_required" and int(context["amount_minor"]) != 0:
            return _custom_outcome("unknown", "payment", "non-zero checkout returned no_payment_required")
        minor, currency = self._verified_quote(page, str(context["currency"]))
        if minor != int(context["amount_minor"]) or currency != str(context["currency"]).upper():
            return _custom_outcome("unknown", "payment", "completed checkout quote mismatch")
        if not context.get("ack_started"):
            if read_only:
                return _custom_outcome(
                    "pending",
                    "checkout",
                    "checkout paid; ack required to activate subscription",
                    intent_status="succeeded",
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
        if self._subscription_active():
            return _custom_outcome(
                "paid", "payment", "checkout completed and subscription active", intent_status="succeeded"
            )
        return _custom_outcome(
            "pending" if context.get("ack_complete") else "unknown",
            "payment",
            "payment confirmed; awaiting subscription activation",
            intent_status="succeeded",
        )

    def _reconcile_or_confirm_intent(self, context: dict[str, Any], *, read_only: bool) -> dict[str, Any]:
        paid = self._reconcile_paid_checkout(context, read_only=read_only)
        if paid:
            return paid
        intent_id = str(context.get("intent_id") or "")
        if not intent_id or not context.get("intent_secret"):
            return _custom_outcome("unknown", "payment", "intent reference missing; refusing to retry payment")
        intent = self._retrieve_intent(context)
        if not intent or intent.get("id") != intent_id:
            return _custom_outcome("unknown", "payment", "unable to retrieve Stripe intent")
        status = str(intent.get("status") or "")
        if intent_id.startswith("pi_"):
            raw_amt = intent.get("amount")
            if (
                not isinstance(raw_amt, int)
                or isinstance(raw_amt, bool)
                or str(intent.get("currency") or "").upper() != str(context["currency"]).upper()
                or raw_amt != int(context["amount_minor"])
            ):
                return _custom_outcome("unknown", "payment", "Stripe intent amount/currency mismatch")
        if status == "requires_confirmation" and not context.get("stripe_confirmation_started"):
            if read_only:
                return _custom_outcome(
                    "pending", "checkout", "intent ready; run without read_only to confirm", intent_status=status
                )
            context["stripe_confirmation_started"] = True
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
            return self._reconcile_or_confirm_intent(context, read_only=read_only)
        if status in {"requires_payment_method", "canceled"}:
            err = _stripe_error_from_intent(intent)
            msg = (
                decline_message(err["decline_code"]) if err.get("decline_code") else err.get("message", "card declined")
            )
            return _custom_outcome(
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
            return _custom_outcome(
                "challenge_required",
                "payment",
                "Stripe requires manual verification (3DS/challenge); complete at checkout_url",
                intent_status=status,
                next_action={"type": str(na.get("type") or "requires_action")},
                checkout_url=str(context.get("checkout_url") or ""),
            )
        if status in {"succeeded", "processing", "requires_capture"}:
            return _custom_outcome(
                "pending", "payment", "payment processing; awaiting checkout settlement", intent_status=status
            )
        return _custom_outcome("unknown", "payment", "unverified intent state", intent_status=status)

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
                    processor=str(spec.get("processor") or processor_from_checkout_url(checkout_url) or "openai_llc"),
                    customer_session_client_secret=str(spec.get("customer_session_client_secret") or ""),
                    confirm_return_url=str(spec.get("confirm_return_url") or ""),
                )
            if not str(context.get("checkout_id") or "").startswith("oaics_"):
                return _custom_outcome(
                    "integration_error",
                    "checkout",
                    "CustomCheckoutProtocolClient requires an oaics_ session",
                    code="checkout_kind_unsupported",
                )
            if context.get("app_confirmation_started"):
                return self._reconcile_or_confirm_intent(context, read_only=read_only)
            original = self._read_checkout(context)
            if original.get("status") == "complete":
                return self._reconcile_paid_checkout(context, read_only=read_only) or _custom_outcome(
                    "unknown", "payment", "checkout complete; payment state unverified"
                )
            if original.get("status") != "open" or original.get("payment_status") != "unpaid":
                return _custom_outcome("request_rejected", "checkout", "checkout is not open and unpaid")
            if context.get("token_started") and not context.get("confirmation_token"):
                return _custom_outcome(
                    "unknown", "setup", "confirmation token creation unverified; refusing duplicate submission"
                )
            if read_only:
                return _custom_outcome("pending", "checkout", "checkout open and unpaid (read-only inspection)")
            resolved_billing = _resolve_billing(billing, country=str((billing or {}).get("country") or "US"))
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
                return _custom_outcome(
                    "pending", "checkout", "taxes changed payable amount; confirm updated quote before paying"
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
            return _custom_outcome("unknown", exc.error_stage or stage, str(exc)[:240], code=exc.error_code)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            return _custom_outcome("unknown", stage, f"upstream failure ({type(exc).__name__}); verify before retrying")


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
