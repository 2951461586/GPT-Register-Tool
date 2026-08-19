"""Shared request and response contracts for ChatGPT Checkout and Stripe init."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .payment_catalog import PAYMENT_METHODS as CATALOG_PAYMENT_METHODS


CHECKOUT_PATH = "/backend-api/payments/checkout"
CHECKOUT_URL = f"https://chatgpt.com{CHECKOUT_PATH}"
STRIPE_INIT_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}/init"
DEFAULT_STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)


class CheckoutContractError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "checkout_contract_invalid",
        error_stage: str = "checkout_contract",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.error_stage = error_stage
        self.retryable = retryable


@dataclass(frozen=True)
class PaymentMethodProfile:
    key: str
    stripe_type: str
    country: str
    currency: str
    payment_locale: str
    browser_locale: str
    browser_timezone: str


@dataclass(frozen=True)
class BrowserProfile:
    browser_locale: str
    browser_timezone: str


# Egress country -> browser locale/timezone presented to Stripe.  A checkout
# created from a JP exit must not advertise an unrelated timezone, so callers
# that pick a country at runtime resolve the pair here instead of hardcoding one.
COUNTRY_BROWSER_PROFILES: dict[str, BrowserProfile] = {
    "US": BrowserProfile("en-US", "America/New_York"),
    "CA": BrowserProfile("en-CA", "America/Toronto"),
    "BR": BrowserProfile("pt-BR", "America/Sao_Paulo"),
    "GB": BrowserProfile("en-GB", "Europe/London"),
    "IE": BrowserProfile("en-IE", "Europe/Dublin"),
    "DE": BrowserProfile("de-DE", "Europe/Berlin"),
    "FR": BrowserProfile("fr-FR", "Europe/Paris"),
    "NL": BrowserProfile("nl-NL", "Europe/Amsterdam"),
    "CH": BrowserProfile("de-CH", "Europe/Zurich"),
    "PL": BrowserProfile("pl-PL", "Europe/Warsaw"),
    "TR": BrowserProfile("tr-TR", "Europe/Istanbul"),
    "IN": BrowserProfile("en-IN", "Asia/Kolkata"),
    "SG": BrowserProfile("en-SG", "Asia/Singapore"),
    "TH": BrowserProfile("th-TH", "Asia/Bangkok"),
    "ID": BrowserProfile("id-ID", "Asia/Jakarta"),
    "ES": BrowserProfile("es-ES", "Europe/Madrid"),
    "PH": BrowserProfile("en-PH", "Asia/Manila"),
    "VN": BrowserProfile("vi-VN", "Asia/Ho_Chi_Minh"),
    "KR": BrowserProfile("ko-KR", "Asia/Seoul"),
    "JP": BrowserProfile("ja-JP", "Asia/Tokyo"),
    "AU": BrowserProfile("en-AU", "Australia/Sydney"),
    "NZ": BrowserProfile("en-NZ", "Pacific/Auckland"),
}
DEFAULT_BROWSER_PROFILE = BrowserProfile("en-US", "America/New_York")


def browser_profile_for_country(country: Any) -> BrowserProfile:
    """Resolve the browser locale/timezone pair advertised for an egress country."""
    key = str(country or "").strip().upper()
    return COUNTRY_BROWSER_PROFILES.get(key, DEFAULT_BROWSER_PROFILE)


PAYMENT_METHOD_PROFILES: dict[str, PaymentMethodProfile] = {
    key: PaymentMethodProfile(
        key,
        definition.stripe_type,
        definition.country,
        definition.currency,
        definition.payment_locale,
        browser_profile_for_country(definition.country).browser_locale,
        browser_profile_for_country(definition.country).browser_timezone,
    )
    for key, definition in CATALOG_PAYMENT_METHODS.items()
}


@dataclass(frozen=True)
class CheckoutRequestContract:
    payment_method: str
    billing_country: str
    currency: str
    payment_locale: str
    browser_locale: str
    browser_timezone: str
    entry_point: str = "all_plans_pricing_modal"
    plan_name: str = "chatgptplusplan"
    promo_campaign_id: str = "plus-1-month-free"
    checkout_ui_mode: str = "custom"

    @classmethod
    def for_payment_method(
        cls,
        payment_method: str,
        *,
        billing_country: str = "",
        currency: str = "",
        payment_locale: str = "",
        browser_locale: str = "",
        browser_timezone: str = "",
        promo_campaign_id: str = "plus-1-month-free",
        checkout_ui_mode: str = "custom",
    ) -> "CheckoutRequestContract":
        key = str(payment_method or "").strip().lower().replace("-", "_")
        profile = PAYMENT_METHOD_PROFILES.get(key)
        if profile is None:
            raise CheckoutContractError(f"unsupported payment method contract: {payment_method}")
        contract = cls(
            payment_method=profile.key,
            billing_country=str(billing_country or profile.country).strip().upper(),
            currency=str(currency or profile.currency).strip().upper(),
            payment_locale=str(payment_locale or profile.payment_locale).strip(),
            browser_locale=str(browser_locale or profile.browser_locale).strip(),
            browser_timezone=str(browser_timezone or profile.browser_timezone).strip(),
            promo_campaign_id=str(promo_campaign_id or "").strip(),
            checkout_ui_mode=str(checkout_ui_mode or "custom").strip(),
        )
        contract.validate()
        return contract

    @property
    def stripe_payment_method(self) -> str:
        return PAYMENT_METHOD_PROFILES[self.payment_method].stripe_type

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Z]{2}", self.billing_country):
            raise CheckoutContractError("billing_country must be a two-letter country code")
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise CheckoutContractError("currency must be a three-letter currency code")
        if self.checkout_ui_mode not in {"custom", "hosted"}:
            raise CheckoutContractError("checkout_ui_mode must be custom or hosted")

    def checkout_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entry_point": self.entry_point,
            "plan_name": self.plan_name,
            "billing_details": {
                "country": self.billing_country,
                "currency": self.currency,
            },
            "checkout_ui_mode": self.checkout_ui_mode,
        }
        if self.promo_campaign_id:
            payload["promo_campaign"] = {
                "promo_campaign_id": self.promo_campaign_id,
                "is_coupon_from_query_param": False,
            }
        return payload

    def stripe_init_payload(
        self,
        publishable_key: str,
        *,
        stripe_version: str = DEFAULT_STRIPE_VERSION,
        stripe_js_id: str = "",
    ) -> dict[str, str]:
        key = str(publishable_key or "").strip()
        if not key.startswith("pk_"):
            raise CheckoutContractError(
                "checkout response did not contain a Stripe publishable key",
                error_code="checkout_publishable_key_missing",
                error_stage="checkout_response",
            )
        return {
            "browser_locale": self.browser_locale,
            "browser_timezone": self.browser_timezone,
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id or str(uuid.uuid4()),
            "elements_session_client[locale]": self.payment_locale,
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": key,
            "_stripe_version": stripe_version,
        }


@dataclass(frozen=True)
class CheckoutSessionContract:
    checkout_session_id: str
    processor_entity: str
    publishable_key: str

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        billing_country: str,
        fallback_publishable_key: str = "",
    ) -> "CheckoutSessionContract":
        if not isinstance(payload, dict):
            raise CheckoutContractError(
                "checkout response must be a JSON object",
                error_code="checkout_bad_response",
                error_stage="checkout_response",
                retryable=True,
            )
        session_id = str(
            payload.get("checkout_session_id") or payload.get("session_id") or payload.get("id") or ""
        ).strip()
        if not session_id.startswith(("cs_", "oaics_")):
            raise CheckoutContractError(
                "checkout response did not contain a supported session id",
                error_code="checkout_session_missing",
                error_stage="checkout_response",
                retryable=True,
            )
        processor = str(payload.get("processor_entity") or "").strip()
        if not processor:
            processor = "openai_llc" if billing_country.upper() == "US" else "openai_ie"
        publishable_key = str(payload.get("publishable_key") or fallback_publishable_key or "").strip()
        return cls(session_id, processor, publishable_key)


@dataclass(frozen=True)
class StripeCapabilityEvidence:
    amount_minor: int | None
    currency: str
    currency_present: bool
    payment_method_types: tuple[str, ...]
    ordered_payment_method_types: tuple[str, ...]
    custom_payment_methods: tuple[str, ...]
    offer_state: str

    @classmethod
    def from_payload(cls, payload: Any, *, fallback_currency: str = "") -> "StripeCapabilityEvidence":
        if not isinstance(payload, dict):
            raise CheckoutContractError(
                "Stripe init response must be a JSON object",
                error_code="stripe_init_bad_response",
                error_stage="stripe_init",
                retryable=True,
            )
        standard = _collect_method_group(payload, "payment_method_types")
        ordered = _collect_method_group(payload, "ordered_payment_method_types")
        custom = _collect_method_group(payload, "custom_payment_methods")
        methods = tuple(_dedupe((*standard, *ordered, *custom)))
        amount = _extract_amount_minor(payload)
        raw_currency = _extract_currency(payload)
        currency = raw_currency or str(fallback_currency or "").upper()
        offer_state = "zero_due" if amount == 0 else "nonzero_due" if amount is not None else "unknown_amount"
        return cls(amount, currency, bool(raw_currency), methods, tuple(ordered), tuple(custom), offer_state)

    def classification_for(self, stripe_payment_method: str) -> tuple[str, bool | None]:
        expected = normalize_payment_method_token(stripe_payment_method)
        if expected in self.payment_method_types:
            return "eligible", True
        if self.payment_method_types:
            return "ineligible", False
        return "unknown", None


def normalize_payment_method_token(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "kakao": "kakao_pay",
        "card_payment": "card",
        "direct_card": "card",
        "go_pay": "gopay",
        "grab_pay": "grabpay",
    }
    return aliases.get(token, token)


def _collect_method_group(payload: Any, key: str) -> list[str]:
    values: list[str] = []
    for group in _values_for_key(payload, key):
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str):
                token = normalize_payment_method_token(item)
                if token:
                    values.append(token)
            elif isinstance(item, dict):
                for candidate_key in ("type", "payment_method_type", "name", "id"):
                    candidate = item.get(candidate_key)
                    token = normalize_payment_method_token(candidate)
                    if token:
                        values.append(token)
                        break
    return _dedupe(values)


def _values_for_key(value: Any, target: str, *, depth: int = 0) -> Iterable[Any]:
    if depth > 10:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == target:
                yield item
            if isinstance(item, (dict, list)):
                yield from _values_for_key(item, target, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from _values_for_key(item, target, depth=depth + 1)


def _extract_amount_minor(payload: dict[str, Any]) -> int | None:
    paths = (
        ("total_summary", "due"),
        ("invoice", "amount_due"),
        ("elements_options", "amount"),
        ("payment_intent", "amount"),
        ("amount_due",),
    )
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        parsed = _minor_units(value)
        if parsed is not None:
            return parsed
    return None


def _minor_units(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    if isinstance(value, dict):
        for key in ("amount", "value", "unit_amount", "amount_minor"):
            if key in value and (parsed := _minor_units(value[key])) is not None:
                return parsed
    return None


def _extract_currency(payload: Any) -> str:
    for key in ("currency", "currency_code"):
        for value in _values_for_key(payload, key):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value.strip()):
                return value.strip().upper()
    return ""


def _dedupe(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output
