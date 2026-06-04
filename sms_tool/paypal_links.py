import json
import time
from pathlib import Path

from curl_cffi import requests as curl_requests

from .account_seed import extract_access_token as _access_token
from .account_seed import load_account_seed as _load_seed
from .config import CFG
from .gen_pp_link import generate_payment_link, generate_pp_link
from .paypal_nocard import _follow_stripe_redirect, extract_ba_token
from .storage import upsert_account


def regenerate_paypal_link(email="", session_file="", proxy=None, payment_method="paypal"):
    data, json_path = _load_seed(email=email, session_file=session_file)
    target_email = (email or data.get("email") or "").strip().lower()
    if target_email:
        data["email"] = target_email

    access_token = _access_token(data)
    if not access_token:
        return {"ok": False, "email": target_email, "error": "missing_access_token"}

    old_paypal = data.get("paypal") if isinstance(data.get("paypal"), dict) else {}
    old_paypal_status = str(data.get("paypal_status") or "").strip()
    payment_method = _normalize_payment_method(payment_method)
    paypal = _generate_link(access_token, proxy=proxy, payment_method=payment_method, seed_data=data)
    checkout_unauthorized = _is_checkout_unauthorized(paypal)
    if checkout_unauthorized:
        refreshed = _refresh_seed_session(target_email, json_path, proxy=proxy, stale_access_token=access_token)
        if refreshed.get("ok"):
            data, json_path = _load_seed(email=target_email, session_file=json_path)
            refreshed_token = _access_token(data)
            if refreshed_token and refreshed_token != access_token:
                access_token = refreshed_token
                paypal = _generate_link(access_token, proxy=proxy, payment_method=payment_method, seed_data=data)
            else:
                paypal = dict(paypal)
                paypal["refresh_error"] = refreshed.get("error", "refresh_returned_same_access_token")
        else:
            paypal = dict(paypal)
            paypal["refresh_error"] = refreshed.get("error", "refresh_failed")
    if _should_resolve_ba_redirect(payment_method, paypal):
        paypal = _resolve_ba_redirect(paypal, proxy=proxy)
    if (
        _requires_ba_token(payment_method)
        and paypal.get("ok")
        and not _is_pm_created(paypal)
        and not _is_chatgpt_checkout_link(paypal)
        and not extract_ba_token(str(paypal.get("url") or ""))
    ):
        paypal = dict(paypal)
        paypal["ok"] = False
        paypal["error"] = paypal.get("ba_resolve_error") or "PayPal BA token was not resolved"
        paypal["error_code"] = "paypal_ba_token_missing"
        paypal["terminal"] = True
        paypal["retryable"] = False
    now = int(time.time())
    if _is_pm_created(paypal):
        data["paypal"] = paypal
        data["paypal_status"] = "pm_created"
        data.pop("paypal_regenerate_error", None)
        data.pop("paypal_regenerate_error_code", None)
        data.pop("paypal_regenerate_error_details", None)
    elif paypal.get("ok") and paypal.get("url"):
        data["paypal"] = paypal
        data["paypal_status"] = "link_ready"
    else:
        can_reuse_old_link = _saved_link_matches_payment_method(old_paypal, payment_method, _payment_cfg(payment_method))
        if can_reuse_old_link:
            data["paypal"] = old_paypal
            data["paypal_status"] = old_paypal_status or "link_ready"
        else:
            if old_paypal.get("url"):
                data["previous_paypal"] = old_paypal
            data["paypal"] = paypal
            data["paypal_status"] = "failed"
        data["paypal_regenerate_error"] = _payment_error(paypal)
        if isinstance(paypal.get("stripe_error"), dict):
            data["paypal_regenerate_error_details"] = paypal["stripe_error"]
        elif isinstance(paypal.get("confirm_summary"), dict):
            data["paypal_regenerate_error_details"] = paypal["confirm_summary"]
        if paypal.get("error_code"):
            data["paypal_regenerate_error_code"] = paypal["error_code"]
    if checkout_unauthorized and not paypal.get("ok") and _at_refresh_failed(paypal):
        data["status"] = "at_invalid"
        data["error"] = _payment_error(paypal) or "access_token_invalidated"
    data["paypal_updated_at"] = now
    data["payment_method"] = payment_method
    data["access_token"] = access_token
    data["success"] = bool(data.get("success", True))

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)

    success = bool((paypal.get("ok") and paypal.get("url")) or _is_pm_created(paypal))
    return {
        "ok": success,
        "email": data.get("email", ""),
        "paypal_status": data["paypal_status"],
        "paypal_url": data.get("paypal", {}).get("url", "") if isinstance(data.get("paypal"), dict) else "",
        "payment_method": payment_method,
        "pm_id": data.get("paypal", {}).get("pm_id", "") if isinstance(data.get("paypal"), dict) else "",
        "json_path": json_path,
        "error": "" if success else _payment_error(paypal),
    }


def _is_pm_created(paypal) -> bool:
    if not isinstance(paypal, dict) or not paypal.get("ok"):
        return False
    link_type = str(paypal.get("link_type") or paypal.get("source") or paypal.get("status") or paypal.get("paypal_status") or "").strip().lower()
    return bool(str(paypal.get("pm_id") or "").startswith("pm_") and link_type in {"pm_created", "stripe_payment_method"})


def _is_checkout_unauthorized(paypal) -> bool:
    if not isinstance(paypal, dict) or paypal.get("ok"):
        return False
    if str(paypal.get("error_code") or "") == "checkout_unauthorized":
        return True
    error = str(paypal.get("error") or "")
    return "401" in error and "checkout" in error.lower()


def _at_refresh_failed(paypal) -> bool:
    text = (
        str(paypal.get("error") or "")
        + " "
        + str(paypal.get("refresh_error") or "")
    ).lower()
    markers = (
        "token_invalidated",
        "token_expired",
        "authentication token has been invalidated",
        "could not validate your token",
        "add_phone_required",
        "oauth_refresh_http_401",
        "cookie_refresh_returned_same_access_token",
        "passwordless_fallback=",
    )
    return any(marker in text for marker in markers)


def _normalize_payment_method(value):
    value = str(value or "").strip().lower()
    if value in {"gopay", "go-pay", "go_pay"}:
        return "gopay"
    return "paypal"


def _payment_cfg(payment_method="paypal"):
    if _normalize_payment_method(payment_method) == "paypal":
        return CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    method_cfg = CFG.get(payment_method) if isinstance(CFG.get(payment_method), dict) else {}
    return method_cfg


def _generate_link(access_token, proxy=None, payment_method="paypal", seed_data=None):
    auth_context = seed_data if isinstance(seed_data, dict) else None
    if _normalize_payment_method(payment_method) == "paypal":
        return generate_pp_link(access_token, proxy=proxy, auth_context=auth_context)
    return generate_payment_link(access_token, proxy=proxy, payment_method=payment_method, auth_context=auth_context)


def _is_chatgpt_checkout_link(paypal):
    if not isinstance(paypal, dict):
        return False
    link_type = str(paypal.get("link_type") or paypal.get("source") or "").strip().lower()
    if link_type in {"chatgpt_checkout", "openai_checkout"}:
        return True
    url = str(paypal.get("url") or paypal.get("checkout_url") or "").strip().lower()
    return (
        "chatgpt.com/checkout/" in url
        or "pay.openai.com/c/pay/" in url
        or "checkout.stripe.com/c/pay/" in url
    )


def _should_resolve_ba_redirect(payment_method, paypal):
    if _normalize_payment_method(payment_method) != "paypal":
        return False
    if not isinstance(paypal, dict) or not paypal.get("ok") or not paypal.get("url"):
        return False
    if _is_chatgpt_checkout_link(paypal):
        return False
    paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    return bool(paypal_cfg.get("resolve_ba_redirect", True))


def _requires_ba_token(payment_method):
    if _normalize_payment_method(payment_method) != "paypal":
        return False
    paypal_cfg = CFG.get("paypal") if isinstance(CFG.get("paypal"), dict) else {}
    return bool(paypal_cfg.get("require_ba_token", False))


def _saved_link_matches_payment_method(paypal, payment_method, payment_cfg=None):
    if not isinstance(paypal, dict) or not paypal.get("url"):
        return False
    target = _normalize_payment_method(payment_method)
    if target == "paypal" and not _saved_paypal_link_matches_target_mode(paypal, payment_cfg or {}):
        return False
    raw_method = str(paypal.get("payment_method") or paypal.get("method") or paypal.get("type") or "").strip().lower()
    method = _normalize_payment_method(raw_method) if raw_method else ""
    currency = str(paypal.get("currency") or "").strip().lower()
    pm_types = paypal.get("payment_method_types")
    if isinstance(pm_types, (list, tuple)):
        pm_type_values = {str(item or "").strip().lower() for item in pm_types}
    else:
        pm_type_values = {str(pm_types or "").strip().lower()} if pm_types else set()
    has_gopay = "gopay" in pm_type_values
    has_paypal = "paypal" in pm_type_values
    if target == "gopay":
        return method == "gopay" or has_gopay or currency == "idr"
    if method == "gopay" or has_gopay or currency == "idr":
        return False
    return method == "paypal" or has_paypal or currency == "usd" or not (raw_method or pm_type_values or currency)


def _saved_paypal_link_matches_target_mode(paypal, payment_cfg):
    raw_url = str(paypal.get("url") or "").strip()
    if not raw_url:
        return False
    if bool(payment_cfg.get("require_ba_token", False)) and not extract_ba_token(raw_url):
        return False
    url = raw_url.lower()
    mode = str(payment_cfg.get("link_mode") or payment_cfg.get("paypal_link_mode") or "stripe_redirect").strip().lower().replace("-", "_")
    checkout_ui_mode = str(payment_cfg.get("checkout_ui_mode") or "custom").strip().lower()
    resolve_ba = bool(payment_cfg.get("resolve_ba_redirect", True))
    raw_redirect_format = str(payment_cfg.get("redirect_url_format") or "").strip().lower().replace("-", "_")
    if raw_redirect_format in {"any", "legacy", "all"}:
        redirect_format = "any"
    elif raw_redirect_format in {"paypal", "paypal_approve", "ba", "ba_approve"}:
        redirect_format = "paypal_approve"
    elif raw_redirect_format:
        redirect_format = "stripe_authorize"
    else:
        redirect_format = "paypal_approve" if resolve_ba else "stripe_authorize"
    if mode in {"stripe", "stripe_confirm", "stripe_redirect", "paypal_redirect", "ba_redirect", "legacy"}:
        if redirect_format == "any":
            return "pm-redirects.stripe.com/authorize" in url or "paypal.com/agreements/approve" in url or "ba_token=" in url
        if redirect_format == "paypal_approve":
            return "paypal.com/agreements/approve" in url or "ba_token=" in url
        return "pm-redirects.stripe.com/authorize" in url
    if mode == "chatgpt_checkout":
        if checkout_ui_mode == "hosted":
            return "pay.openai.com/c/pay/" in url or "checkout.stripe.com/c/pay/" in url
        return "chatgpt.com/checkout/" in url
    return True


def _refresh_seed_session(email, json_path, proxy=None, stale_access_token=""):
    from .session_refresh import refresh_session

    print(f"[*] Trying cookie-based session refresh for {email}...")
    protocol_result = {"ok": False, "error": "refresh_not_attempted"}
    try:
        protocol_result = refresh_session(
            email=email or "",
            session_file=json_path or "",
            timeout=60,
            proxy=proxy,
        )
        if protocol_result.get("ok"):
            refreshed_data, _ = _load_seed(email=email, session_file=json_path)
            refreshed_token = _access_token(refreshed_data)
            if not stale_access_token or (refreshed_token and refreshed_token != stale_access_token):
                print(f"[*] Cookie-based refresh succeeded.")
                return protocol_result
            protocol_result = dict(protocol_result)
            protocol_result["error"] = "cookie_refresh_returned_same_access_token"
            print("[*] Cookie-based refresh returned the same access token; trying OAuth refresh token fallback.")
        else:
            print(f"[*] Cookie-based refresh failed: {protocol_result.get('error', 'unknown')}")
    except Exception as exc:
        protocol_result = {"ok": False, "error": str(exc)}
        print(f"[*] Cookie-based refresh exception: {exc}")

    print(f"[*] Trying OAuth refresh token fallback for {email}...")
    oauth_result = _try_oauth_refresh_token(email, json_path, proxy=proxy, stale_access_token=stale_access_token)
    if oauth_result.get("ok"):
        return oauth_result
    print(f"[*] OAuth refresh token fallback failed: {oauth_result.get('error', 'unknown')}")

    print(f"[*] Trying passwordless email OTP login fallback for {email}...")
    login_result = _try_passwordless_oauth_login(email, json_path, proxy=proxy, stale_access_token=stale_access_token)
    if login_result.get("ok"):
        return login_result
    print(f"[*] Passwordless email OTP login fallback failed: {login_result.get('error', 'unknown')}")
    return {
        "ok": False,
        "error": (
            f"{protocol_result.get('error', 'refresh_failed')}; "
            f"oauth_fallback={oauth_result.get('error', 'unknown')}; "
            f"passwordless_fallback={login_result.get('error', 'unknown')}"
        ),
        "protocol_error": protocol_result.get("error", ""),
        "oauth_error": oauth_result.get("error", ""),
        "passwordless_error": login_result.get("error", ""),
    }


def _try_oauth_refresh_token(email, json_path, proxy=None, stale_access_token=""):
    data, _ = _load_seed(email=email, session_file=json_path)
    refresh_token = _extract_refresh_token(data)
    if not refresh_token:
        return {"ok": False, "error": "no_oauth_refresh_token"}

    auth_base = CFG.get("chatgpt", {}).get("auth_base_url", "https://auth.openai.com").rstrip("/")
    client_id = (
        str((CFG.get("chatgpt") or {}).get("codex_oauth_client_id") or "").strip()
        or "app_EMoamEEZ73f0CkXaXp7hrann"
    )
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        response = session.post(
            f"{auth_base}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/148.0.0.0 Safari/537.36",
            },
            impersonate="chrome",
            timeout=30,
        )
        body = response.json() if response.text else {}
    except Exception as exc:
        return {"ok": False, "error": f"oauth_refresh_request_failed: {exc}"}
    if response.status_code >= 400:
        return {"ok": False, "error": f"oauth_refresh_http_{response.status_code}: {json.dumps(body, ensure_ascii=False)[:300]}"}

    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        return {"ok": False, "error": "oauth_refresh_missing_access_token"}
    if stale_access_token and access_token == stale_access_token:
        return {"ok": False, "error": "oauth_refresh_returned_same_access_token"}

    new_refresh_token = str(body.get("refresh_token") or refresh_token).strip()
    now = int(time.time())
    data["access_token"] = access_token
    data["oauth_refresh_token"] = new_refresh_token
    data["refresh_token_status"] = "oauth_present"
    data["refresh_token_updated_at"] = now
    data["refreshed_at"] = now
    data["refresh_mode"] = "oauth_refresh_token"

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)

    print(f"[*] OAuth refresh token succeeded for {email or data.get('email', '')}")
    return {
        "ok": True,
        "mode": "oauth_refresh_token",
        "email": data.get("email", ""),
        "json_path": json_path,
    }


def _try_passwordless_oauth_login(email, json_path, proxy=None, stale_access_token=""):
    data, _ = _load_seed(email=email, session_file=json_path)
    try:
        from .codex_oauth import refresh_codex_oauth_session

        result = refresh_codex_oauth_session(
            data,
            json_path=json_path,
            proxy=proxy,
            timeout=180,
            force_email_otp_login=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"passwordless_login_exception:{exc}"}
    if not result.get("ok"):
        return result
    refreshed_data, _ = _load_seed(email=email, session_file=json_path)
    refreshed_token = _access_token(refreshed_data)
    if stale_access_token and refreshed_token == stale_access_token:
        return {"ok": False, "error": "passwordless_login_returned_same_access_token"}
    if not refreshed_token:
        return {"ok": False, "error": "passwordless_login_missing_access_token"}
    print(f"[*] Passwordless email OTP login fallback succeeded for {email or refreshed_data.get('email', '')}")
    return {
        "ok": True,
        "mode": "passwordless_email_otp_login",
        "email": refreshed_data.get("email", email or ""),
        "json_path": json_path,
    }


def _payment_error(paypal):
    error = str(paypal.get("error") or "").strip() if isinstance(paypal, dict) else ""
    refresh_error = str(paypal.get("refresh_error") or "").strip() if isinstance(paypal, dict) else ""
    if refresh_error and refresh_error not in error:
        return (error + "; " if error else "") + f"refresh_error: {refresh_error}"
    return error


def _extract_refresh_token(data):
    candidates = [
        data.get("oauth_refresh_token"),
        data.get("refresh_token"),
    ]
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    candidates.extend([
        auth_session.get("refreshToken"),
        auth_session.get("refresh_token"),
    ])
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    candidates.extend([
        session.get("refreshToken"),
        session.get("refresh_token"),
    ])
    for value in candidates:
        token = str(value or "").strip()
        if token and len(token) > 10:
            return token
    return ""


def _resolve_ba_redirect(paypal, proxy=None):
    url = str(paypal.get("url") or "").strip()
    if not url or extract_ba_token(url):
        return paypal
    try:
        resolved = _follow_stripe_redirect(
            url,
            proxy=proxy,
            log=lambda message: print(f"[paypal] resolve BA: {message}"),
        )
    except Exception as exc:
        paypal["ba_resolve_error"] = str(exc)
        return paypal
    if extract_ba_token(resolved):
        paypal = dict(paypal)
        paypal["stripe_redirect_url"] = url
        paypal["url"] = resolved
        paypal["ba_resolved"] = True
    else:
        paypal = dict(paypal)
        paypal["ba_resolve_error"] = "missing_ba_token"
        paypal["ba_resolve_final_url"] = resolved
    return paypal


