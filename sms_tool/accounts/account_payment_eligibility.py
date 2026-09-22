"""Post-registration payment-eligibility probe (payment methods per account).

Answers "which payment rails can this account actually use?" by creating a
throwaway Checkout and calling Stripe init, then reading the full
``payment_method_types`` list Stripe returns for that billing country.  The
probe stops before payment-method creation -- the side-effect boundary is owned
by :mod:`sms_tool.payment_capability`.

Operator decisions (2026-09-21):

* **Display** -- appended to the desktop 优惠状态 column, e.g.
  ``可试用Plus-100% · card/upi/momo``.  The composition happens in the display
  layer (``AccountStatusInterpreter.DisplayPromotionStatus``), not in
  ``promotion_status``: that field stays the pure promotion label, which is why
  ``desktop_read._promotion_presentation`` had to strip a legacy
  ``｜可支付:`` suffix in the first place.
* **Scope** -- one full enumeration per account, not one probe per method.
  Stripe returns the whole enabled-method list for the checkout session, so a
  single Checkout + init covers card / link / apple_pay / upi / momo at once.
* **Default** -- enabled.  Callers pass ``payment_eligibility=False`` to opt out.

🔴 The method list is **billing-country dependent**.  Probing an IN account
through a US exit returns the US method list and silently reports
``ineligible`` for UPI -- a wrong answer, not a failure.  Keep the probe exit
consistent with ``billing_country`` and check ``proxy_source`` in the result.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from .. import payment_capability
from ..checkout_contract import browser_profile_for_country
from ..paypal_extract import CURRENCY_MAP
from ..promotion_states import (
    MAX_ELIGIBILITY_LABEL_TOKENS,
    PAYMENT_ELIGIBILITY_UNKNOWN_LABEL,
    payment_eligibility_label,
    payment_method_tokens,
)
from .account_identity import account_identity

logger = logging.getLogger(__name__)

# The probe only needs *a* valid Checkout contract to reach Stripe init: the
# method list that comes back describes the whole checkout session, not the
# requested method.  ``direct_card`` is the generic card contract and the only
# catalog entry whose flow has no provider redirect host, so it is the least
# likely to be rejected before Stripe init runs.
ELIGIBILITY_CARRIER_METHOD = "direct_card"

DEFAULT_BILLING_COUNTRY = "US"

# Re-exported for callers that only need the badge formatting rule; the
# implementation is owned by ``promotion_states`` so the desktop read path can
# use it without importing the payment catalog.
MAX_LABEL_TOKENS = MAX_ELIGIBILITY_LABEL_TOKENS

# ``paypal_extract.CURRENCY_MAP`` is the canonical country->currency map, but it
# predates the PH/VN/PL/CH/ES/NL entries in ``payment_methods.json``.  The
# extras live here instead of mutating the PayPal lane's map, because that map
# feeds ``self.currency = CURRENCY_MAP.get(target_country, "EUR")`` and adding
# keys would silently change its EUR fallback.
_EXTRA_BILLING_CURRENCY: dict[str, str] = {
    "PH": "PHP",
    "VN": "VND",
    "PL": "PLN",
    "CH": "CHF",
    "ES": "EUR",
    "NL": "EUR",
}


def _account_token(account: Any) -> str:
    if isinstance(account, str):
        return account.strip()
    if isinstance(account, Mapping):
        return str(account.get("access_token") or "").strip()
    return ""


def billing_country_for(account: Any) -> str:
    """Billing country to enumerate methods for.

    Prefers the account's own registration country so the answer describes the
    rail this account was created for; falls back to the US default.
    """
    if not isinstance(account, Mapping):
        return DEFAULT_BILLING_COUNTRY
    for key in ("registration_country", "billing_country", "country"):
        value = str(account.get(key) or "").strip().upper()
        if len(value) == 2 and value.isalpha():
            return value
    return DEFAULT_BILLING_COUNTRY


def billing_currency_for(country: Any) -> str:
    """ISO-4217 currency for a billing country, defaulting to USD."""
    code = str(country or "").strip().upper()
    if not code:
        return "USD"
    if code in _EXTRA_BILLING_CURRENCY:
        return _EXTRA_BILLING_CURRENCY[code]
    return str(CURRENCY_MAP.get(code) or "USD").strip().upper()


def payment_locale_for(country: Any) -> str:
    """Stripe Elements locale for a billing country (primary language subtag)."""
    locale = browser_profile_for_country(country).browser_locale
    return str(locale or "en").split("-", 1)[0] or "en"


def _auth_context(account: Any, *, device_id: str) -> dict[str, Any]:
    if not isinstance(account, Mapping):
        return {"device_id": device_id, "oai_did": device_id}
    return {
        "email": str(account.get("email") or ""),
        "device_id": device_id,
        "oai_did": device_id,
        "cookie_header": str(account.get("cookie_header") or ""),
    }


def probe_account_payment_eligibility(
    account: Any,
    *,
    proxy: Any = None,
    timeout: int = 45,
    country: str = "",
    promo_campaign_id: str = "",
    require_zero: bool = False,
) -> dict[str, Any]:
    """Enumerate the payment methods Stripe offers this account.

    Side-effect free: Checkout + Stripe init only, never a payment-method
    creation, confirm or approve.  Returns a plain dict that is safe to persist
    into ``raw_json`` -- no token, cookie or proxy material is echoed back.
    """
    token = _account_token(account)
    if not token:
        return {
            "ok": False,
            "error": "missing_access_token",
            "error_code": "missing_access_token",
            "error_stage": "validation",
            "retryable": False,
        }

    target_country = str(country or "").strip().upper() or billing_country_for(account)

    identity = account_identity(account) if isinstance(account, Mapping) else {}
    device_id = str(
        identity.get("device_id")
        or (account.get("device_id") if isinstance(account, Mapping) else "")
        or ""
    )
    proxy_text = _proxy_text(proxy)

    kwargs: dict[str, Any] = {
        "auth_context": _auth_context(account, device_id=device_id),
        "proxy": proxy_text,
        "billing_country": target_country,
        "checkout_country": target_country,
        "currency": billing_currency_for(target_country),
        "payment_locale": payment_locale_for(target_country),
        "require_zero": bool(require_zero),
        "timeout": max(5, int(timeout or 45)),
    }
    browser = browser_profile_for_country(target_country)
    kwargs["browser_locale"] = browser.browser_locale
    kwargs["browser_timezone"] = browser.browser_timezone
    if promo_campaign_id:
        kwargs["promo_campaign_id"] = promo_campaign_id

    try:
        raw = payment_capability.payment_method_capability_probe(
            access_token=token,
            payment_method=ELIGIBILITY_CARRIER_METHOD,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never break a batch
        logger.debug("payment eligibility probe raised", exc_info=True)
        return {
            "ok": False,
            "error": str(exc)[:300],
            "error_code": "eligibility_probe_exception",
            "error_stage": "payment_eligibility",
            "retryable": True,
        }

    return _normalize_probe(raw, target_country=target_country)


def _normalize_probe(raw: Mapping[str, Any], *, target_country: str) -> dict[str, Any]:
    """Keep only the enumerable, token-free fields the desktop needs."""
    if not isinstance(raw, Mapping):
        return {
            "ok": False,
            "error": "eligibility_probe_bad_result",
            "error_code": "eligibility_probe_bad_result",
            "error_stage": "payment_eligibility",
            "retryable": True,
        }
    standard = _token_tuple(raw.get("payment_method_types"))
    ordered = _token_tuple(raw.get("ordered_payment_method_types"))
    custom = _token_tuple(raw.get("custom_payment_methods"))
    # ``ordered`` is Stripe's own display order and is the better default, but a
    # response may carry only one of the two groups.
    primary = ordered or standard
    merged = _dedupe((*primary, *standard, *custom))

    result: dict[str, Any] = {
        "ok": bool(raw.get("ok")) and bool(merged),
        "billing_country": target_country,
        "carrier_method": ELIGIBILITY_CARRIER_METHOD,
        "currency": str(raw.get("currency") or "").strip().upper(),
        "amount": raw.get("amount"),
        "offer_state": str(raw.get("offer_state") or ""),
        "payment_method_types": list(standard),
        "ordered_payment_method_types": list(ordered),
        "custom_payment_methods": list(custom),
        "methods": list(merged),
        "error": str(raw.get("error") or ""),
        "error_code": str(raw.get("error_code") or ""),
        "error_stage": str(raw.get("error_stage") or ""),
        "retryable": bool(raw.get("retryable")),
    }
    if not result["ok"] and not result["error"]:
        result["error"] = str(raw.get("decision") or "payment_methods_unavailable")
        result["error_code"] = result["error_code"] or result["error"]
        result["error_stage"] = result["error_stage"] or "payment_eligibility"
    return result


def _token_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item or "").strip().lower() for item in value if str(item or "").strip())


def _dedupe(values: Any) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return tuple(output)


def _proxy_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("https") or value.get("http") or ""
    return str(value or "").strip()


__all__ = [
    "DEFAULT_BILLING_COUNTRY",
    "ELIGIBILITY_CARRIER_METHOD",
    "MAX_LABEL_TOKENS",
    "PAYMENT_ELIGIBILITY_UNKNOWN_LABEL",
    "billing_country_for",
    "billing_currency_for",
    "payment_eligibility_label",
    "payment_locale_for",
    "payment_method_tokens",
    "probe_account_payment_eligibility",
]
