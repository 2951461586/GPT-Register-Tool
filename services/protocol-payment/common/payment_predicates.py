"""Pure payment-payload and redirect-URL predicates shared by the extractors.

Stage-1 batch of
``docs/audits/plan-2026-09-17-protocol-payment-extractor-consolidation.md``:
eight of these were byte-identical copies in ``ideal_qr_extract.py`` and
``twint_extract.py``; ``is_redirect_like_url`` differed only in provider data
(the redirect host suffix and the URL marker), so it takes the
:class:`ProviderProfile` instead of being duplicated -- the same shape
``provider_profile.py`` already uses for locales/currencies.

Nothing here performs I/O or reads extractor module state, so the functions are
safe to unit-test directly and cannot create an import cycle back into an
extractor.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .protocol_core import collect_urls
from .provider_profile import ProviderProfile
from .proxy_bookkeeping import is_known_static_host

__all__ = [
    "checkout_response_has_promo",
    "checkout_response_has_trial",
    "extract_qr_candidates",
    "is_approve_failure_error",
    "is_checkout_not_active_error",
    "is_qr_candidate",
    "is_redirect_like_url",
    "is_resource_url",
    "should_retry_second_confirm_after_approve",
]


def is_checkout_not_active_error(value: Any) -> bool:
    return "checkout_not_active_session" in str(value)


def checkout_response_has_promo(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in (
        "scheduled_discount_preview",
        "immediate_discount_settings",
        "promo_campaign",
        "promo_credit_grant",
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def checkout_response_has_trial(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    # pi-lens-ignore: no-identity-operator-on-literals
    if payload.get("one_click_trial_eligible") is True:
        return True
    subscription_data = payload.get("subscription_data")
    # pi-lens-ignore: unchecked-throwing-call-python
    if isinstance(subscription_data, dict) and int(subscription_data.get("trial_period_days") or 0) > 0:
        return True
    for key in ("trial_period_days", "trial_end"):
        value = payload.get(key)
        if value not in (None, "", 0, "0", False):
            return True
    return False


def is_resource_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    if is_known_static_host(url):
        return True
    return path.endswith(
        (
            ".js",
            ".css",
            ".map",
            ".woff",
            ".woff2",
            ".ttf",
            ".otf",
            ".ico",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
        )
    )


def is_redirect_like_url(profile: ProviderProfile, url: str, from_action_field: bool = False) -> bool:
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return False
    if is_resource_url(url):
        return False
    if from_action_field:
        return True

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    text = f"{host}{path}?{query}"
    if host in {"hooks.stripe.com", "payments.stripe.com"}:
        return True
    if host.endswith(f".{profile.redirect_domain}") or host == profile.redirect_domain:
        return True
    return any(part in text for part in (profile.redirect_marker, "/redirect/", "redirect_to_url", "authenticate"))


def is_qr_candidate(url: str) -> bool:
    lower = url.lower()
    return lower.startswith("data:image/") or "qr" in lower or "qrcode" in lower or "qr-code" in lower


def extract_qr_candidates(payload: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in collect_urls(payload):
        if url in seen:
            continue
        seen.add(url)
        if is_qr_candidate(url) and not is_known_static_host(url):
            result.append(url)
    return result


def should_retry_second_confirm_after_approve(error: Any) -> bool:
    text = str(error or "").lower()
    return (
        "checkout_upcoming_invoice_mismatch" in text
        or "redirect url resolution timeout" in text
        or "missing_redirect" in text
    )


def is_approve_failure_error(error: str) -> bool:
    text = str(error or "").lower()
    return "approve" in text or "chatgpt approve" in text
