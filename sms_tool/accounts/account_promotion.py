"""ChatGPT account plan / promotion (优惠) detection.

Probes ``/backend-api/accounts/check/v4-2023-04-27`` with a saved access token and
extracts the account's current plan plus any Plus-trial / discount eligibility.
Referenced from the turb-gpt-free-register plan-check flow, adapted to this
project's curl_cffi + auth-header stack. The condensed ``promotion_status`` label
is what the desktop 优惠状态 column shows; the full parse is persisted for detail.

This module owns the **probe only**. Running it across many saved accounts --
proxy rotation, 429 throttling, the optional payment-eligibility pass,
persistence and progress events -- lives in
:mod:`sms_tool.accounts.promotion_batch`. The split keeps the payment catalog off
the desktop read path, which imports this module.
"""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as curl_requests

from .account_identity import (
    access_token_of,
    account_chatgpt_id,
    bind_account_identity,
    chatgpt_account_id_from_token,
)
from ..auth_headers import auth_impersonate, chatgpt_headers
from ..config import CFG
from ..phone_proxy import normalize_proxy_url, redact_proxy_url as _redact_proxy_url
from ..promotion_states import (
    PROMOTION_STATE_AUTH_INVALID,
    PROMOTION_STATE_FREE,
    PROMOTION_STATE_PROBE_FAILED,
    PROMOTION_STATE_SUBSCRIBED,
    PROMOTION_STATE_TRIAL_ELIGIBLE,
    PROMOTION_STATE_UNKNOWN,
)
from ..proxy_routing import select_operation_proxy_candidate

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNTS_CHECK_URL = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}"


def _account_token(account: Any) -> str:
    """Back-compat shim; the canonical reader is ``account_identity.access_token_of``."""
    return access_token_of(account)


def parse_accounts_check(body: Any, *, account_id: str = "") -> dict[str, Any]:
    """Extract plan + Plus-trial/discount eligibility from an accounts/check body."""
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(accounts, dict):
        return {"ok": False, "error": "accounts_check_missing_accounts"}
    item = None
    if account_id and isinstance(accounts.get(account_id), dict):
        item = accounts.get(account_id)
    elif isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
    else:
        item = next((v for k, v in accounts.items() if k != "default" and isinstance(v, dict)), None)
    if not isinstance(item, dict):
        return {"ok": False, "error": "accounts_check_no_entry"}

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    promo = item.get("eligible_promo_campaigns") or {}
    plus_campaign = promo.get("plus") if isinstance(promo, dict) else None
    plus_meta = (plus_campaign or {}).get("metadata") or {}
    discount = plus_meta.get("discount") or {}
    duration = plus_meta.get("duration") or {}

    plan_type = str(account.get("plan_type") or "").strip()
    subscription_plan = str(entitlement.get("subscription_plan") or "").strip()
    has_active = bool(entitlement.get("has_active_subscription"))
    is_free = plan_type.lower() == "free" or subscription_plan.lower() == "chatgptfreeplan"
    plus_trial_eligible = bool(is_free and plus_campaign)
    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    return {
        "ok": True,
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "plus_trial_eligible": plus_trial_eligible,
        "plus_trial_campaign_id": (plus_campaign or {}).get("id"),
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_discount_percentage": discount.get("percentage"),
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "eligible_offer_ids": eligible_offer_ids,
    }


def promotion_status_label(result: dict[str, Any]) -> str:
    """Condense a parsed result into the compact 优惠状态 badge text."""
    if not isinstance(result, dict) or not result.get("ok"):
        error = str((result or {}).get("error") or "").lower()
        if "401" in error or "token" in error or "unauthorized" in error:
            return "AT失效"
        return "检测失败"
    plan = str(result.get("current_plan_type") or "").strip().lower()
    if result.get("has_active_subscription") and plan and plan != "free":
        label = "Plus" if "plus" in plan else (plan or "已订阅")
        return f"{label.capitalize()}(赠)" if result.get("is_active_subscription_gratis") else f"已订阅·{label}"
    if result.get("plus_trial_eligible"):
        pct = result.get("plus_trial_discount_percentage")
        periods = result.get("plus_trial_duration_num_periods")
        period = str(result.get("plus_trial_duration_period") or "").strip()
        parts = ["可试用Plus"]
        if pct not in (None, ""):
            try:
                parts.append(f"-{int(round(float(pct)))}%")
            except (TypeError, ValueError):
                pass
        if periods not in (None, "") and period:
            parts.append(f"×{periods}{period}")
        return "·".join(parts)
    return "Free·无优惠"


def promotion_status_code(result: Any) -> str:
    """Machine state for the 优惠 badge, from the same parsed result as
    :func:`promotion_status_label`.

    Persisted as ``promotion_state`` next to the display label so the desktop
    filter/sort consumes a stable enum (see ``sms_tool/promotion_states.py``)
    instead of substring-matching Chinese copy.
    """
    if not isinstance(result, dict) or not result:
        return PROMOTION_STATE_UNKNOWN
    if not result.get("ok"):
        error = str(result.get("error") or "").lower()
        if "401" in error or "token" in error or "unauthorized" in error:
            return PROMOTION_STATE_AUTH_INVALID
        return PROMOTION_STATE_PROBE_FAILED
    plan = str(result.get("current_plan_type") or "").strip().lower()
    if result.get("has_active_subscription") and plan and plan != "free":
        return PROMOTION_STATE_SUBSCRIBED
    if result.get("plus_trial_eligible"):
        return PROMOTION_STATE_TRIAL_ELIGIBLE
    return PROMOTION_STATE_FREE


def check_account_promotion(
    account: Any,
    proxy: str | None = None,
    timeout: int = 20,
    timezone_offset_min: str = "-",
    *,
    browser_fetch: Any = None,
    proxy_pool: str | list[str] | None = None,
) -> dict[str, Any]:
    """Probe accounts/check for one account and return plan + promotion detail.

    When ``browser_fetch`` is provided, the probe is routed through the
    browser context's ``fetch_json`` method instead of ``curl_cffi``,
    carrying the real browser fingerprint and cookies to bypass
    Cloudflare-based 401 blocks on protocol-only requests.
    """
    token = _account_token(account)
    if not token:
        return {"ok": False, "promotion_status": "缺少AT", "error": "missing_access_token", "promotion_state": PROMOTION_STATE_PROBE_FAILED}

    had_identity_context = bool(account.get("identity_context")) if isinstance(account, dict) else False
    identity = bind_account_identity(account)
    # Promotion checks must reuse the saved registration egress/fingerprint
    # pair; presenting the same AT from a different exit can trigger revocation.
    selected_proxy = select_operation_proxy_candidate(
        account if had_identity_context else {key: value for key, value in account.items() if key != "identity_context"},
        operation="promotion",
        explicit=proxy or proxy_pool,
        config=CFG,
    )
    resolved_proxy = selected_proxy.proxy if selected_proxy else None
    proxy_source = selected_proxy.source if selected_proxy else "direct"

    account_id = account_chatgpt_id(account) if isinstance(account, dict) else chatgpt_account_id_from_token(token)
    did = str(identity.get("device_id") or (account.get("device_id") if isinstance(account, dict) else "") or "")
    headers = chatgpt_headers(did, accept="*/*", referer="https://chatgpt.com/")
    headers["Authorization"] = f"Bearer {token}"
    headers["oai-language"] = "en-US"
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    url = f"{ACCOUNTS_CHECK_URL}?timezone_offset_min={timezone_offset_min}"

    # When a browser fetch callable is provided, route the probe through the
    # browser context to carry the real fingerprint and cookies.
    retry_after = ""
    if browser_fetch is not None:
        try:
            result = browser_fetch(url, headers=headers, timeout_ms=timeout * 1000)
            # ``PlaywrightBrowserSession.fetch_json`` returns the HTTP status
            # under the ``status`` key, not ``status_code``.  Normalize the same
            # way ``account_liveness.probe_account_liveness`` does, otherwise a
            # genuine response is discarded as a transport failure and every
            # browser-routed promotion probe degrades to "HTTP 0".
            if isinstance(result, dict) and "status_code" not in result and "status" in result:
                result = {**result, "status_code": result.get("status")}
            if isinstance(result, dict) and "status_code" in result:
                status_code = int(result.get("status_code") or 0)
                body = result.get("body")
            else:
                status_code = 0
                body = result
        except Exception as exc:
            return {"ok": False, "promotion_status": "检测失败", "error": str(exc)[:300], "promotion_state": PROMOTION_STATE_PROBE_FAILED, "proxy_source": proxy_source}
    else:
        normalized_proxy = normalize_proxy_url(resolved_proxy)
        proxies = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else None
        try:
            response = curl_requests.get(
                url, headers=headers, proxies=proxies, timeout=timeout,
                impersonate=auth_impersonate(), allow_redirects=False,
            )
        except Exception as exc:
            error = str(exc)
            for candidate in (str(proxy or "").strip(), str(resolved_proxy or "").strip(), normalized_proxy):
                if candidate:
                    error = error.replace(candidate, _redact_proxy_url(candidate, empty_placeholder=""))
            return {"ok": False, "promotion_status": "检测失败", "error": error[:300], "promotion_state": PROMOTION_STATE_PROBE_FAILED, "proxy_source": proxy_source}
        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            retry_after = str((getattr(response, "headers", None) or {}).get("Retry-After") or "").strip()
        except Exception:
            retry_after = ""
        try:
            body = response.json()
        except Exception:
            return {"ok": False, "promotion_status": "检测失败", "error": "invalid_json", "status_code": status_code, "promotion_state": PROMOTION_STATE_PROBE_FAILED, "proxy_source": proxy_source}

    if status_code == 401:
        return {"ok": False, "promotion_status": "AT失效", "error": "token_invalid", "status_code": 401, "promotion_state": PROMOTION_STATE_AUTH_INVALID, "proxy_source": proxy_source}
    if not (200 <= status_code < 300):
        failure = {
            "ok": False,
            "promotion_status": f"HTTP {status_code}",
            "error": f"http_{status_code}",
            "status_code": status_code,
            "promotion_state": PROMOTION_STATE_PROBE_FAILED,
            "proxy_source": proxy_source,
        }
        if retry_after:
            failure["retry_after"] = retry_after
        return failure

    parsed = parse_accounts_check(body, account_id=account_id)
    parsed["status_code"] = status_code
    parsed["promotion_status"] = promotion_status_label(parsed)
    parsed["promotion_state"] = promotion_status_code(parsed)
    parsed["proxy_source"] = proxy_source
    return parsed


__all__ = [
    "ACCOUNTS_CHECK_PATH",
    "ACCOUNTS_CHECK_URL",
    "check_account_promotion",
    "parse_accounts_check",
    "promotion_status_code",
    "promotion_status_label",
]
