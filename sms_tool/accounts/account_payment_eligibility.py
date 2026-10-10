"""Post-registration payment-eligibility probe (payment methods per account).

Observes explicit payment methods for one billing-country Checkout session.
``cs_*`` uses Stripe init and optional Elements specs; ``oaics_*`` uses
custom Checkout evidence. It stops before payment-method creation -- the
side-effect boundary is owned by :mod:`sms_tool.payment_capability`.

Operator decisions (2026-09-21):

* **Display** -- appended to the desktop 优惠状态 column, e.g.
  ``可试用Plus-100% · card/upi/momo``. Composition is owned by
  ``promotion_states.promotion_status_with_eligibility``; the desktop reads
  ``promotion_display`` without parsing the label.
* **Scope** -- one full enumeration per account, not one probe per method.
  the list describes this Checkout session, not a future payment guarantee.
* **Default** -- opt in to disposable Checkout separately from a plan check.

🔴 The method list is **billing-country dependent**. The egress gate checks
the actual exit before Checkout; unknown or mismatched exits produce an
unknown result instead of a potentially false method list.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Mapping

from .. import payment_capability
from .. import payment_egress
from ..checkout_contract import browser_profile_for_country
from ..payment_wire import CURRENCY_MAP
from ..payment_routing import parse_proxy_pool, payment_proxy_pools
from ..proxy_entry import retarget_region
from ..config import CFG
from .. import storage
from ..promotion_states import (
    MAX_ELIGIBILITY_LABEL_TOKENS,
    PAYMENT_ELIGIBILITY_UNKNOWN_LABEL,
    payment_eligibility_label,
    payment_method_tokens,
)
from .account_identity import access_token_of, account_identity

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

# ``payment_wire.CURRENCY_MAP`` is the canonical country->currency map, but it
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

_ELIGIBILITY_STAGES = frozenset({
    "validation", "preparing_proxy", "checkout_create", "checkout_response",
    "stripe_init", "stripe_elements", "custom_checkout", "payment_eligibility",
    "capability_probe", "checkout_contract", "capability_classification",
})
_ELIGIBILITY_ERROR_CODES = frozenset({
    "missing_access_token", "egress_country_unverified", "egress_country_mismatch",
    "egress_probe_failed", "checkout_risk_blocked", "checkout_creation_rate_limited",
    "checkout_unauthorized", "checkout_failed", "checkout_transport_failed",
    "checkout_identity_mismatch",
    "checkout_failed_bad_json", "checkout_session_invalid", "checkout_contract_invalid",
    "stripe_init_failed", "stripe_init_unauthorized", "stripe_init_transport_failed",
    "stripe_init_failed_bad_json", "stripe_elements_failed", "stripe_elements_unauthorized",
    "stripe_elements_failed_bad_json", "custom_checkout_failed", "custom_checkout_unauthorized",
    "custom_checkout_failed_bad_json", "custom_checkout_unavailable",
    "custom_checkout_transport_failed", "checkout_currency_mismatch",
    "checkout_currency_unknown", "checkout_amount_unknown", "nonzero_offer",
    "payment_method_unavailable", "payment_methods_unavailable",
    "eligibility_probe_exception", "eligibility_probe_bad_result", "eligibility_probe_failed",
    "capability_probe_unexpected",
})


def payment_eligibility_diagnostics(observations: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Aggregate fixed, non-sensitive failure facts; never copy upstream text."""
    stages: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    for item in observations:
        if not isinstance(item, dict) or item.get("ok"):
            continue
        stage = str(item.get("error_stage") or "")
        code = str(item.get("error_code") or "")
        stages[stage if stage in _ELIGIBILITY_STAGES else "other"] += 1
        codes[code if code in _ELIGIBILITY_ERROR_CODES else "other"] += 1
        status = item.get("http_status")
        if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599:
            statuses[str(status)] += 1
        country = str(item.get("billing_country") or "").upper()
        countries[country if re.fullmatch(r"[A-Z]{2}", country) else "unknown"] += 1
    return {
        "by_stage": dict(sorted(stages.items())),
        "by_error_code": dict(sorted(codes.items())),
        "by_http_status": dict(sorted(statuses.items())),
        "by_billing_country": dict(sorted(countries.items())),
    }


def _account_token(account: Any) -> str:
    """Back-compat shim; the canonical reader is ``account_identity.access_token_of``."""
    return access_token_of(account)


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

    Creates a disposable Checkout and calls Stripe init, but never creates a
    payment method, confirms or approves a payment. Returns a plain dict safe
    to persist into ``raw_json`` -- no token, cookie or proxy material is echoed.
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
    # Only rewrite known provider country templates. An opaque proxy stays
    # untouched and must still pass the observed-exit gate below.
    proxy_text = retarget_region(_proxy_text(proxy), target_country)
    # Stripe's methods depend on the actual exit, not merely the country
    # requested in the Checkout payload. A missing or mismatched exit cannot
    # provide evidence of this account's eligibility.
    if not proxy_text:
        return {
            "ok": False,
            "billing_country": target_country,
            "error_code": "egress_country_unverified",
            "error_stage": "preparing_proxy",
            "retryable": True,
        }
    try:
        payment_egress.assert_egress_countries(
            {
                "checkout_proxy": proxy_text,
                "stage_proxy_countries": {"checkout": target_country},
            },
            stages=("checkout",),
        )
    except payment_egress.EgressCheckError as exc:
        return {
            "ok": False,
            "billing_country": target_country,
            "error_code": exc.error_code,
            "error_stage": "preparing_proxy",
            "expected_country": exc.expected_country,
            "observed_country": exc.observed_country,
            "retryable": exc.retryable,
        }
    except Exception:
        logger.debug("payment eligibility egress check failed")
        return {
            "ok": False,
            "billing_country": target_country,
            "error_code": "egress_probe_failed",
            "error_stage": "preparing_proxy",
            "retryable": True,
        }

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
    except Exception:  # noqa: BLE001 - a probe must never break a batch
        logger.debug("payment eligibility probe raised")
        return {
            "ok": False,
            "error": "eligibility_probe_exception",
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

    currency_mismatch = str(raw.get("decision") or raw.get("error_code") or "") == "checkout_currency_mismatch"
    if currency_mismatch or not bool(raw.get("ok")):
        standard, ordered, custom, merged = (), (), (), ()

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
        # Upstream error text may contain a URL, proxy credential or token.
        # Only persist/report locally owned, fixed diagnostic identifiers.
        "error": "",
        "error_code": "",
        "error_stage": "",
        "http_status": raw.get("http_status") if isinstance(raw.get("http_status"), int) else 0,
        "retryable": bool(raw.get("retryable")),
        "checkout_kind": raw.get("checkout_kind") if isinstance(raw.get("checkout_kind"), str) and raw["checkout_kind"] in {"stripe", "oaics"} else "",
        "evidence_sources": [
            source for source in (raw.get("evidence_sources") or [])
            if isinstance(source, str) and source in {"stripe_init", "stripe_elements", "custom_checkout"}
        ] if isinstance(raw.get("evidence_sources"), (list, tuple)) else [],
    }
    raw_code = str(raw.get("error_code") or raw.get("decision") or "")
    raw_stage = str(raw.get("error_stage") or "")
    if not result["ok"]:
        result["error_code"] = raw_code if raw_code in _ELIGIBILITY_ERROR_CODES else "eligibility_probe_failed"
        result["error_stage"] = raw_stage if raw_stage in _ELIGIBILITY_STAGES else "payment_eligibility"
        result["error"] = result["error_code"]
    return result


def _token_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    tokens = (str(item or "").strip().lower() for item in value)
    return tuple(
        token for token in tokens
        if re.fullmatch(r"[a-z][a-z0-9_]{0,31}", token) and not token.startswith("cpmt_")
    )


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


def probe_payment_eligibility_statuses(
    emails: list[str],
    *,
    proxy: str | None = None,
    proxy_pool: str | list[str] | None = None,
    timeout: int = 45,
) -> dict[str, Any]:
    """Explicit, serial Checkout observations; never perform a plan or payment action."""
    candidates = list(dict.fromkeys(
        ([proxy] if proxy else [])
        + parse_proxy_pool(proxy_pool)
        + payment_proxy_pools(CFG, ELIGIBILITY_CARRIER_METHOD)["checkout"]
    )) or [None]
    results = []
    for email in dict.fromkeys(str(item or "").strip().lower() for item in emails):
        if not email:
            continue
        record = storage.get_account_record(email)
        if not record:
            results.append({"email": email, "ok": False, "error_code": "account_not_found", "persisted": False})
            continue
        try:
            account = json.loads(record.get("raw_json") or "{}")
        except (TypeError, ValueError):
            account = {}
        if not isinstance(account, dict):
            account = {}
        account["access_token"] = record.get("access_token") or account.get("access_token") or ""
        observation = {}
        for candidate in candidates:
            observation = probe_account_payment_eligibility(account, proxy=candidate, timeout=timeout)
            if observation.get("error_stage") != "preparing_proxy":
                break
        persisted = bool(storage.mark_payment_capability(email, observation))
        results.append({
            "email": email,
            "ok": bool(observation.get("ok")),
            "persisted": persisted,
            "payment_capability": observation,
            "payment_eligibility": payment_eligibility_label(observation),
        })
    observations = [item["payment_capability"] for item in results if "payment_capability" in item]
    success = sum(bool(item["ok"]) for item in results)
    return {
        "ok": bool(results) and success == len(results) and all(item["persisted"] for item in results),
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "persist_failed": sum(not item["persisted"] for item in results),
        "payment_eligibility_diagnostics": payment_eligibility_diagnostics(observations),
        "results": results,
    }


__all__ = [
    "DEFAULT_BILLING_COUNTRY",
    "ELIGIBILITY_CARRIER_METHOD",
    "MAX_LABEL_TOKENS",
    "PAYMENT_ELIGIBILITY_UNKNOWN_LABEL",
    "billing_country_for",
    "billing_currency_for",
    "payment_eligibility_label",
    "payment_eligibility_diagnostics",
    "payment_locale_for",
    "payment_method_tokens",
    "probe_account_payment_eligibility",
    "probe_payment_eligibility_statuses",
]
