"""ChatGPT account plan / promotion (优惠) detection.

Probes ``/backend-api/accounts/check/v4-2023-04-27`` with a saved access token and
extracts the account's current plan plus any Plus-trial / discount eligibility.
Referenced from the turb-gpt-free-register plan-check flow, adapted to this
project's curl_cffi + auth-header stack. The condensed ``promotion_status`` label
is what the desktop 优惠状态 column shows; the full parse is persisted for detail.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from curl_cffi import requests as curl_requests

from .account_identity import account_identity, bind_account_identity
from .account_liveness import account_chatgpt_id, browser_fetch_for_account
from .auth_headers import auth_impersonate, chatgpt_headers
from .config import CFG
from .phone_proxy import normalize_proxy_url, redact_proxy_url as _redact_proxy_url
from .proxy_routing import select_operation_proxy

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNTS_CHECK_URL = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}"


def _account_token(account: Any) -> str:
    if isinstance(account, str):
        return account.strip()
    if isinstance(account, dict):
        return str(account.get("access_token") or "").strip()
    return ""


def _jwt_account_id(token: str) -> str:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return ""
    auth = data.get("https://api.openai.com/auth") if isinstance(data, dict) else {}
    if isinstance(auth, dict):
        return str(auth.get("chatgpt_account_id") or "").strip()
    return ""


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


def check_account_promotion(
    account: Any,
    proxy: str | None = None,
    timeout: int = 20,
    timezone_offset_min: str = "-",
    *,
    browser_fetch: Any = None,
) -> dict[str, Any]:
    """Probe accounts/check for one account and return plan + promotion detail.

    When ``browser_fetch`` is provided, the probe is routed through the
    browser context's ``fetch_json`` method instead of ``curl_cffi``,
    carrying the real browser fingerprint and cookies to bypass
    Cloudflare-based 401 blocks on protocol-only requests.
    """
    token = _account_token(account)
    if not token:
        return {"ok": False, "promotion_status": "缺少AT", "error": "missing_access_token"}

    had_identity_context = bool(account.get("identity_context")) if isinstance(account, dict) else False
    identity = bind_account_identity(account)
    # Promotion checks are separate account operations; do not reuse a saved
    # browser-registration exit that may now be blocked or contaminated.
    resolved_proxy = select_operation_proxy(
        account if had_identity_context else {key: value for key, value in account.items() if key != "identity_context"},
        operation="promotion",
        explicit=proxy,
        config=CFG,
    )

    account_id = account_chatgpt_id(account) if isinstance(account, dict) else _jwt_account_id(token)
    did = str(identity.get("device_id") or (account.get("device_id") if isinstance(account, dict) else "") or "")
    headers = chatgpt_headers(did, accept="*/*", referer="https://chatgpt.com/")
    headers["Authorization"] = f"Bearer {token}"
    headers["oai-language"] = "en-US"
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    url = f"{ACCOUNTS_CHECK_URL}?timezone_offset_min={timezone_offset_min}"

    # When a browser fetch callable is provided, route the probe through the
    # browser context to carry the real fingerprint and cookies.
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
            return {"ok": False, "promotion_status": "检测失败", "error": str(exc)[:300]}
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
            return {"ok": False, "promotion_status": "检测失败", "error": error[:300]}
        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            body = response.json()
        except Exception:
            return {"ok": False, "promotion_status": "检测失败", "error": "invalid_json", "status_code": status_code}

    if status_code == 401:
        return {"ok": False, "promotion_status": "AT失效", "error": "token_invalid", "status_code": 401}
    if not (200 <= status_code < 300):
        return {"ok": False, "promotion_status": f"HTTP {status_code}", "error": f"http_{status_code}", "status_code": status_code}

    parsed = parse_accounts_check(body, account_id=account_id)
    parsed["status_code"] = status_code
    parsed["promotion_status"] = promotion_status_label(parsed)
    return parsed


def refresh_promotion_statuses(
    emails: list[str] | None = None,
    workers: int = 4,
    proxy: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """Probe plan/promotion for saved accounts and persist ``promotion_status``."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .storage import get_account_record, list_paypal_accounts, mark_promotion_status

    requested = [str(e or "").strip().lower() for e in (emails or []) if str(e or "").strip()]
    if not requested:
        requested = [str(row.get("email") or "").strip().lower() for row in list_paypal_accounts()]
    requested = list(dict.fromkeys(e for e in requested if e))
    accounts: list[dict[str, Any]] = []
    for email in requested:
        record = get_account_record(email)
        data: dict[str, Any] = {"email": email}
        if record:
            try:
                data.update(json.loads(record.get("raw_json") or "{}"))
            except Exception:
                pass
            data.setdefault("access_token", record.get("access_token") or "")
        accounts.append(data)

    max_workers = max(1, min(int(workers or 1), 16, len(accounts) or 1))
    results: list[dict[str, Any]] = []
    run_id = uuid.uuid4().hex
    _emit_account_batch_event(run_id, "batch_started", "running", total=len(accounts), detail="账号优惠检测开始")

    def run(account: dict[str, Any]) -> dict[str, Any]:
        email = str(account.get("email") or "").strip().lower()
        try:
            with browser_fetch_for_account(account, proxy=proxy, timeout=timeout) as browser_fetch:
                probe = check_account_promotion(
                    account, proxy=proxy, timeout=timeout, browser_fetch=browser_fetch
                )
            label = str(probe.get("promotion_status") or "")
            persisted = mark_promotion_status(email, label, promotion_result=probe) if email else False
            result = {"email": email, "ok": bool(probe.get("ok")), "promotion_status": label, "persisted": bool(persisted), "probe": probe}
        except Exception as exc:
            result = {"email": email, "ok": False, "promotion_status": "检测失败", "persisted": False, "probe": {"ok": False, "error": str(exc)[:200]}}
        _emit_account_batch_event(
            run_id,
            "account_completed",
            "completed" if result.get("ok") else "failed",
            account_ref=email,
            total=len(accounts),
            detail=str(result.get("promotion_status") or "检测完成"),
        )
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run, account) for account in accounts]
        for future in as_completed(futures):
            results.append(future.result())

    success = sum(1 for item in results if item.get("ok"))
    _emit_account_batch_event(run_id, "batch_completed", "completed", total=len(results), detail=f"完成 {len(results)} 个账号")
    return {
        "ok": success == len(results) if results else False,
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "results": results,
    }


def _emit_account_batch_event(
    run_id: str,
    stage: str,
    status: str,
    *,
    account_ref: str = "",
    total: int = 0,
    detail: str = "",
) -> None:
    try:
        from .desktop_ipc import emit_event

        emit_event({
            "domain": "account_promotion",
            "run_id": run_id,
            "account_ref": account_ref,
            "stage": stage,
            "status": status,
            "total": int(total or 0),
            "detail": detail,
        })
    except Exception:
        pass
