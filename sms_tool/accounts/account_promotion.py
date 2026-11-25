"""ChatGPT account plan / promotion (优惠) detection.

Probes ``/backend-api/accounts/check/v4-2023-04-27`` with a saved access token and
extracts the account's current plan plus any Plus-trial / discount eligibility.
Referenced from the turb-gpt-free-register plan-check flow, adapted to this
project's curl_cffi + auth-header stack. The condensed ``promotion_status`` label
is what the desktop 优惠状态 column shows; the full parse is persisted for detail.

This module owns the **probe only**. Running it across many saved accounts --
proxy rotation, 429 throttling, persistence and progress events -- lives in
:mod:`sms_tool.accounts.promotion_batch`.
"""

from __future__ import annotations

import re
from typing import Any, cast
from urllib.parse import quote

from curl_cffi import requests as curl_requests

from .account_identity import (
    access_token_of,
    account_chatgpt_id,
    bind_account_identity,
    chatgpt_account_id_from_token,
)
from ..transport.auth_headers import auth_impersonate, chatgpt_headers
from ..transport import endpoints
from ..config import CFG
from ..phone.phone_proxy import normalize_proxy_url, redact_proxy_url as _redact_proxy_url
from ..payment.promotion_states import (
    PROMOTION_STATE_AUTH_INVALID,
    PROMOTION_STATE_FREE,
    PROMOTION_STATE_PROBE_FAILED,
    PROMOTION_STATE_SUBSCRIBED,
    PROMOTION_STATE_TRIAL_ELIGIBLE,
    PROMOTION_STATE_UNKNOWN,
    TEAM_TRIAL_CAMPAIGN_ID,
)
from ..proxy.routing import select_operation_proxy_candidate

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNTS_CHECK_URL = f"{endpoints.CHATGPT_BASE}{ACCOUNTS_CHECK_PATH}"

#: Read-only Plus-trial coupon probe.  It answers "is the trial still redeemable
#: for this account?" directly, where ``accounts/check`` only exposes an
#: indirect ``eligible_promo_campaigns`` hint.  Never redeems anything.
DEFAULT_COUPON_CAMPAIGN_ID = "plus-1-month-free"
#: Read-only ChatGPT Team 5-seat trial coupon probe (single source in promotion_states).
TEAM_TRIAL_COUPON_CAMPAIGN_ID = TEAM_TRIAL_CAMPAIGN_ID
COUPON_PATH = "/backend-api/promo_campaign/check_coupon"
COUPON_QUERY = f"coupon={DEFAULT_COUPON_CAMPAIGN_ID}&is_coupon_from_query_param=true"
COUPON_URL = f"{endpoints.CHATGPT_BASE}{COUPON_PATH}?{COUPON_QUERY}"
TEAM_COUPON_QUERY = f"coupon={TEAM_TRIAL_COUPON_CAMPAIGN_ID}&is_coupon_from_query_param=true"
TEAM_COUPON_URL = f"{endpoints.CHATGPT_BASE}{COUPON_PATH}?{TEAM_COUPON_QUERY}"
TEAM_COUPON_INELIGIBILITY_REASONS: dict[str, str] = {
    "prior_introductory_offer_redeemed": "此前已兑换过入门优惠",
    "promo_ineligible": "不符合活动资格，未提供更细原因",
}
_UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)


def _accounts_mapping(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    accounts = body.get("accounts")
    if isinstance(accounts, dict):
        return accounts
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
        return data.get("accounts")
    return None


def _extract_personal_account_id(accounts: dict[str, Any], *, fallback_account_id: str = "") -> str:
    """Resolve the personal account id for ``ChatGPT-Account-ID`` coupon probes.

    Mirrors the web console check: collect deduplicated non-``default`` account
    entries (preferring ``account.account_id`` / ``account.id`` / UUID map key),
    pick ``structure == "personal"`` first, and only fall back to the first
    account id when the payload omits ``structure`` entirely.
    """
    unique: dict[str, dict[str, Any]] = {}
    has_structure_field = False
    for key, value in accounts.items():
        if not isinstance(value, dict):
            continue
        account = value.get("account") if isinstance(value.get("account"), dict) else value
        if not isinstance(account, dict):
            continue
        if str(account.get("structure") or "").strip():
            has_structure_field = True
        raw_key = str(key or "").strip()
        aid = str(
            account.get("account_id") or account.get("id") or (raw_key if _UUID_RE.match(raw_key) else "")
        ).strip()
        if not aid or aid == "default":
            continue
        unique[aid] = account
    for aid, account in unique.items():
        if str(account.get("structure") or "").strip().lower() == "personal":
            return aid
    if has_structure_field:
        return ""
    if unique:
        return next(iter(unique))
    fallback = str(fallback_account_id or "").strip()
    return "" if fallback == "default" else fallback


def coupon_url_for_campaign(campaign_id: str = "") -> str:
    """Build the read-only ``check_coupon`` URL for a specific campaign id.

    Falls back to :data:`DEFAULT_COUPON_CAMPAIGN_ID` when ``accounts/check``
    omits a campaign id, while preserving non-default campaigns such as
    ``plus-2-months-50-pct-off`` instead of probing the wrong coupon.
    """
    resolved = str(campaign_id or "").strip() or DEFAULT_COUPON_CAMPAIGN_ID
    return f"{endpoints.CHATGPT_BASE}{COUPON_PATH}?coupon={quote(resolved, safe='')}&is_coupon_from_query_param=true"


def _account_token(account: Any) -> str:
    """Back-compat shim; the canonical reader is ``account_identity.access_token_of``."""
    return access_token_of(account)


def parse_accounts_check(body: Any, *, account_id: str = "") -> dict[str, Any]:
    """Extract plan + Plus-trial/discount eligibility from an accounts/check body."""
    accounts = _accounts_mapping(body)
    if not isinstance(accounts, dict):
        return {"ok": False, "error": "accounts_check_missing_accounts"}
    personal_account_id = _extract_personal_account_id(accounts, fallback_account_id=account_id)
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
    promo_campaigns = (
        {str(key): value for key, value in promo.items() if isinstance(value, dict)}
        if is_free and isinstance(promo, dict)
        else {}
    )
    offers = (item.get("eligible_offers") or {}).get("offers") or []
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    return {
        "ok": True,
        "personal_account_id": personal_account_id,
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "plus_trial_eligible": plus_trial_eligible,
        "plus_trial_campaign_id": (plus_campaign or {}).get("id"),
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_summary": plus_meta.get("summary"),
        "plus_trial_promotion_type_label": plus_meta.get("promotion_type_label"),
        "plus_trial_discount_percentage": discount.get("percentage"),
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "eligible_promo_campaigns": promo_campaigns,
        "eligible_offer_ids": eligible_offer_ids,
    }


def parse_coupon_check(body: Any) -> dict[str, Any]:
    """Extract the trial-coupon state from a ``check_coupon`` body.

    Mirrors the reference ``plus_trial_checker`` and ``team-1-month-free`` check:
    ``state == "eligible"`` is the affirmative answer, and
    ``redemption.redeemed`` is the negative one.
    """
    _raw_payload: object = body
    payload: dict = _raw_payload if isinstance(_raw_payload, dict) else {}
    raw_redemption = payload.get("redemption")
    redemption: dict[str, Any] = raw_redemption if isinstance(raw_redemption, dict) else {}
    state = str(payload.get("state") or payload.get("status") or "").strip().lower()
    return {
        "coupon": str(payload.get("coupon") or "").strip(),
        "coupon_state": state,
        "coupon_redeemed": bool(redemption.get("redeemed")),
        "coupon_trial_eligible": state == "eligible",
        "coupon_ineligibility_reason": str(payload.get("ineligibility_reason") or "").strip(),
        "coupon_redeemed_at": str(
            redemption.get("user_redeemed_at")
            or redemption.get("redeemed_at")
            or redemption.get("workspace_redeemed_at")
            or ""
        ),
        "coupon_expires_at": str(redemption.get("expires_at") or ""),
    }


def _merge_coupon_evidence(parsed: dict[str, Any], coupon: dict[str, Any]) -> None:
    """Fold coupon evidence into an accounts/check parse without overriding plan.

    The coupon endpoint is authoritative for *trial eligibility*; it never
    changes plan or subscription state, so this only ever promotes
    ``plus_trial_eligible`` to True and demotes it on a redeemed coupon.
    """
    parsed["coupon_state"] = str(coupon.get("coupon_state") or "")
    parsed["coupon_redeemed"] = bool(coupon.get("coupon_redeemed"))
    parsed["coupon_trial_eligible"] = bool(coupon.get("coupon_trial_eligible"))
    for key in ("coupon_redeemed_at", "coupon_expires_at", "coupon_campaign_id"):
        if coupon.get(key):
            parsed[key] = coupon[key]
    parsed["plus_trial_hint"] = bool(parsed.get("plus_trial_eligible"))
    parsed["plus_trial_eligible"] = bool(coupon.get("coupon_trial_eligible"))


def _merge_team_coupon_evidence(parsed: dict[str, Any], team_coupon: dict[str, Any]) -> None:
    """Fold ``team-1-month-free`` (5-seat Team trial) coupon evidence into ``parsed``."""
    parsed["team_trial_campaign_id"] = str(team_coupon.get("coupon_campaign_id") or TEAM_TRIAL_COUPON_CAMPAIGN_ID)
    parsed["team_trial_state"] = str(team_coupon.get("coupon_state") or "")
    parsed["team_trial_redeemed"] = bool(team_coupon.get("coupon_redeemed"))
    parsed["team_trial_eligible"] = bool(team_coupon.get("ok") and team_coupon.get("coupon_trial_eligible"))
    reason = str(team_coupon.get("coupon_ineligibility_reason") or "").strip()
    if reason:
        parsed["team_trial_ineligibility_reason"] = reason
        parsed["team_trial_ineligibility_reason_label"] = TEAM_COUPON_INELIGIBILITY_REASONS.get(reason, reason)
    if team_coupon.get("coupon_redeemed_at"):
        parsed["team_trial_redeemed_at"] = str(team_coupon["coupon_redeemed_at"])
    if team_coupon.get("coupon_expires_at"):
        parsed["team_trial_expires_at"] = str(team_coupon["coupon_expires_at"])


def _coupon_probe_worth_running(parsed: dict[str, Any]) -> bool:
    """Only free, subscription-less accounts can have a redeemable Plus trial."""
    if not parsed.get("ok") or parsed.get("has_active_subscription"):
        return False
    return str(parsed.get("current_plan_type") or "").strip().lower() in {"", "free"}


def _probe_coupon_endpoint(
    *,
    headers,
    timeout,
    browser_fetch,
    proxies,
    campaign_id: str,
    error_prefix: str,
    require_coupon_match: bool = False,
) -> dict[str, Any]:
    """Read-only ``check_coupon`` probe on the shared transport/fingerprint stack."""
    url = coupon_url_for_campaign(campaign_id)
    try:
        if browser_fetch is not None:
            result = browser_fetch(url, headers=headers, timeout_ms=int(timeout * 1000))
            if isinstance(result, dict) and "status_code" not in result and "status" in result:
                result = {**result, "status_code": result.get("status")}
            status_code = int(result.get("status_code") or 0) if isinstance(result, dict) else 0
            body = result.get("body") if isinstance(result, dict) else result
        else:
            response = curl_requests.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                impersonate=auth_impersonate(),
                allow_redirects=False,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            try:
                body = response.json()
            except Exception:
                return {
                    "ok": False,
                    "status_code": status_code,
                    "coupon_campaign_id": campaign_id,
                    "error": f"{error_prefix}_invalid_json",
                }
        if status_code != 200 or not isinstance(body, dict):
            return {
                "ok": False,
                "status_code": status_code,
                "coupon_campaign_id": campaign_id,
                "error": f"{error_prefix}_unavailable",
            }
        parsed_coupon = parse_coupon_check(body)
        if require_coupon_match and parsed_coupon.get("coupon") != campaign_id:
            return {
                "ok": False,
                "status_code": status_code,
                "coupon_campaign_id": campaign_id,
                "coupon": parsed_coupon.get("coupon") or "",
                "error": f"{error_prefix}_campaign_mismatch",
            }
        return {"ok": True, "coupon_campaign_id": campaign_id, **parsed_coupon}
    except Exception as exc:
        return {
            "ok": False,
            "coupon_campaign_id": campaign_id,
            "error": f"{error_prefix}_transport:{str(exc)[:200]}",
        }


def _probe_plus_coupon(*, headers, timeout, browser_fetch, proxies, campaign_id: str = "") -> dict[str, Any]:
    """Read-only Plus-trial coupon probe on the same transport/fingerprint as the plan check."""
    resolved_campaign = str(campaign_id or "").strip() or DEFAULT_COUPON_CAMPAIGN_ID
    return _probe_coupon_endpoint(
        headers=headers,
        timeout=timeout,
        browser_fetch=browser_fetch,
        proxies=proxies,
        campaign_id=resolved_campaign,
        error_prefix="plus_coupon",
    )


def _probe_team_coupon(*, headers, timeout, browser_fetch, proxies, account_id: str = "") -> dict[str, Any]:
    """Read-only ChatGPT Team 5-seat trial (``team-1-month-free``) coupon probe."""
    personal_id = str(account_id or "").strip()
    if not personal_id:
        return {
            "ok": False,
            "coupon_campaign_id": TEAM_TRIAL_COUPON_CAMPAIGN_ID,
            "error": "personal_account_not_found",
        }
    req_headers = dict(headers)
    req_headers["Chatgpt-Account-Id"] = personal_id
    result = _probe_coupon_endpoint(
        headers=req_headers,
        timeout=timeout,
        browser_fetch=browser_fetch,
        proxies=proxies,
        campaign_id=TEAM_TRIAL_COUPON_CAMPAIGN_ID,
        error_prefix="team_coupon",
        require_coupon_match=True,
    )
    result["account_id"] = personal_id
    return result


def _apply_coupon_probe(parsed: dict[str, Any], *, headers, timeout, browser_fetch, proxies) -> None:
    """Run the dynamic-campaign Plus coupon probe and fold its verdict into ``parsed``."""
    coupon = _probe_plus_coupon(
        headers=headers,
        timeout=timeout,
        browser_fetch=browser_fetch,
        proxies=proxies,
        campaign_id=str(parsed.get("plus_trial_campaign_id") or ""),
    )
    parsed["coupon"] = coupon
    if coupon.get("ok"):
        _merge_coupon_evidence(parsed, coupon)


def _apply_team_coupon_probe(parsed: dict[str, Any], *, headers, timeout, browser_fetch, proxies) -> None:
    """Run the ``team-1-month-free`` 5-seat trial probe and fold its verdict into ``parsed``."""
    team_coupon = _probe_team_coupon(
        headers=headers,
        timeout=timeout,
        browser_fetch=browser_fetch,
        proxies=proxies,
        account_id=str(parsed.get("personal_account_id") or ""),
    )
    parsed["team_coupon"] = team_coupon
    _merge_team_coupon_evidence(parsed, team_coupon)


def _apply_coupon_probes(
    parsed: dict[str, Any],
    *,
    coupon_probe: bool,
    team_coupon_probe: bool,
    headers,
    timeout,
    browser_fetch,
    proxies,
) -> None:
    if coupon_probe and _coupon_probe_worth_running(parsed):
        _apply_coupon_probe(parsed, headers=headers, timeout=timeout, browser_fetch=browser_fetch, proxies=proxies)
    if team_coupon_probe and parsed.get("ok"):
        _apply_team_coupon_probe(parsed, headers=headers, timeout=timeout, browser_fetch=browser_fetch, proxies=proxies)


def promotion_status_label(result: dict[str, Any]) -> str:
    """Condense a parsed result into the compact 优惠状态 badge text."""
    if not isinstance(result, dict) or not result.get("ok"):
        error = str((result or {}).get("error") or "").lower()
        if "401" in error or "token" in error or "unauthorized" in error:
            return "AT失效"
        return "检测失败"
    plan = str(result.get("current_plan_type") or "").strip().lower()
    team_eligible = bool(result.get("team_trial_eligible"))
    team_tag = "可试用Team(5席)"
    if result.get("has_active_subscription") and plan and plan != "free":
        label = "Plus" if "plus" in plan else (plan or "已订阅")
        base = f"{label.capitalize()}(赠)" if result.get("is_active_subscription_gratis") else f"已订阅·{label}"
        return f"{base}·{team_tag}" if team_eligible else base
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
        if team_eligible:
            parts.append(team_tag)
        return "·".join(parts)
    if team_eligible:
        return team_tag
    return "Free·无优惠"


def promotion_status_code(result: Any) -> str:
    """Machine state for the 优惠 badge, from the same parsed result as
    :func:`promotion_status_label`.

    Persisted as ``promotion_state`` next to the display label so the desktop
    filter/sort consumes a stable enum (see ``sms_tool/payment/promotion_states.py``)
    instead of substring-matching Chinese copy.
    """
    if not isinstance(result, dict) or not result:
        return PROMOTION_STATE_UNKNOWN
    if not result.get("ok"):
        error = str(result.get("error") or "").lower()
        if "401" in error or "token" in error or "unauthorized" in error:
            return PROMOTION_STATE_AUTH_INVALID
        return PROMOTION_STATE_PROBE_FAILED
    if result.get("team_trial_eligible"):
        return PROMOTION_STATE_TRIAL_ELIGIBLE
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
    coupon_probe: bool = False,
    team_coupon_probe: bool = False,
) -> dict[str, Any]:
    """Probe accounts/check + optional Plus/Team trial coupons for one account."""
    token = _account_token(account)
    if not token:
        return {
            "ok": False,
            "promotion_status": "缺少AT",
            "error": "missing_access_token",
            "promotion_state": PROMOTION_STATE_PROBE_FAILED,
        }

    had_identity_context = bool(account.get("identity_context")) if isinstance(account, dict) else False
    identity = bind_account_identity(account)
    # Promotion checks must reuse the saved registration egress/fingerprint
    # pair; presenting the same AT from a different exit can trigger revocation.
    selected_proxy = select_operation_proxy_candidate(
        account
        if had_identity_context
        else {key: value for key, value in account.items() if key != "identity_context"},
        operation="promotion",
        explicit=cast("str | None", proxy or proxy_pool),
        config=CFG,
    )
    resolved_proxy = selected_proxy.proxy if selected_proxy else None
    proxy_source = selected_proxy.source if selected_proxy else "direct"

    account_id = account_chatgpt_id(account) if isinstance(account, dict) else chatgpt_account_id_from_token(token)
    did = str(identity.get("device_id") or (account.get("device_id") if isinstance(account, dict) else "") or "")
    headers = chatgpt_headers(did, accept="*/*", referer=endpoints.CHATGPT_ORIGIN)
    headers["Authorization"] = f"Bearer {token}"
    headers["oai-language"] = "en-US"
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    url = f"{ACCOUNTS_CHECK_URL}?timezone_offset_min={timezone_offset_min}"

    normalized_proxy = normalize_proxy_url(resolved_proxy or "")
    # ``Any`` on purpose: curl_cffi's ``ProxySpec`` is a ``TypedDict``, which a
    # plain ``dict[str, str]`` is not assignable to, while the runtime accepts it.
    proxies: Any = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else None

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
            return {
                "ok": False,
                "promotion_status": "检测失败",
                "error": str(exc)[:300],
                "promotion_state": PROMOTION_STATE_PROBE_FAILED,
                "proxy_source": proxy_source,
            }
    else:
        try:
            response = curl_requests.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                impersonate=auth_impersonate(),
                allow_redirects=False,
            )
        except Exception as exc:
            error = str(exc)
            for candidate in (str(proxy or "").strip(), str(resolved_proxy or "").strip(), normalized_proxy):
                if candidate:
                    error = error.replace(candidate, _redact_proxy_url(candidate, empty_placeholder=""))
            return {
                "ok": False,
                "promotion_status": "检测失败",
                "error": error[:300],
                "promotion_state": PROMOTION_STATE_PROBE_FAILED,
                "proxy_source": proxy_source,
            }
        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            retry_after = str((getattr(response, "headers", None) or {}).get("Retry-After") or "").strip()
        except Exception:
            retry_after = ""
        try:
            body = response.json()
        except Exception:
            return {
                "ok": False,
                "promotion_status": "检测失败",
                "error": "invalid_json",
                "status_code": status_code,
                "promotion_state": PROMOTION_STATE_PROBE_FAILED,
                "proxy_source": proxy_source,
            }

    if status_code == 401:
        return {
            "ok": False,
            "promotion_status": "AT失效",
            "error": "token_invalid",
            "status_code": 401,
            "promotion_state": PROMOTION_STATE_AUTH_INVALID,
            "proxy_source": proxy_source,
        }
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
    _apply_coupon_probes(
        parsed,
        coupon_probe=coupon_probe,
        team_coupon_probe=team_coupon_probe,
        headers=headers,
        timeout=timeout,
        browser_fetch=browser_fetch,
        proxies=proxies,
    )
    parsed["promotion_status"] = promotion_status_label(parsed)
    parsed["promotion_state"] = promotion_status_code(parsed)
    parsed["proxy_source"] = proxy_source
    return parsed


__all__ = [
    "ACCOUNTS_CHECK_PATH",
    "ACCOUNTS_CHECK_URL",
    "COUPON_PATH",
    "COUPON_URL",
    "DEFAULT_COUPON_CAMPAIGN_ID",
    "TEAM_COUPON_INELIGIBILITY_REASONS",
    "TEAM_COUPON_URL",
    "TEAM_TRIAL_COUPON_CAMPAIGN_ID",
    "check_account_promotion",
    "coupon_url_for_campaign",
    "parse_accounts_check",
    "parse_coupon_check",
    "promotion_status_code",
    "promotion_status_label",
]
