import json
import time
from pathlib import Path

from curl_cffi import requests as curl_requests

from .config import CFG
from .gen_pp_link import generate_payment_link, generate_pp_link
from .paypal_nocard import _follow_stripe_redirect, extract_ba_token
from .storage import get_account_record, upsert_account


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
    paypal = _generate_link(access_token, proxy=proxy, payment_method=payment_method)
    if _is_checkout_unauthorized(paypal):
        refreshed = _refresh_seed_session(target_email, json_path, proxy=proxy, stale_access_token=access_token)
        if refreshed.get("ok"):
            data, json_path = _load_seed(email=target_email, session_file=json_path)
            refreshed_token = _access_token(data)
            if refreshed_token and refreshed_token != access_token:
                access_token = refreshed_token
                paypal = _generate_link(access_token, proxy=proxy, payment_method=payment_method)
            else:
                paypal = dict(paypal)
                paypal["refresh_error"] = refreshed.get("error", "refresh_returned_same_access_token")
        else:
            paypal = dict(paypal)
            paypal["refresh_error"] = refreshed.get("error", "refresh_failed")
    if payment_method == "paypal" and paypal.get("ok") and paypal.get("url"):
        paypal = _resolve_ba_redirect(paypal, proxy=proxy)
    now = int(time.time())
    if paypal.get("ok") and paypal.get("url"):
        data["paypal"] = paypal
        data["paypal_status"] = "link_ready"
    else:
        can_reuse_old_link = _saved_link_matches_payment_method(old_paypal, payment_method)
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
        if paypal.get("error_code"):
            data["paypal_regenerate_error_code"] = paypal["error_code"]
    data["paypal_updated_at"] = now
    data["payment_method"] = payment_method
    data["access_token"] = access_token
    data["success"] = bool(data.get("success", True))

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(data, json_path=json_path)

    return {
        "ok": bool(paypal.get("ok") and paypal.get("url")),
        "email": data.get("email", ""),
        "paypal_status": data["paypal_status"],
        "paypal_url": data.get("paypal", {}).get("url", "") if isinstance(data.get("paypal"), dict) else "",
        "payment_method": payment_method,
        "json_path": json_path,
        "error": _payment_error(paypal),
    }


def _is_checkout_unauthorized(paypal) -> bool:
    if not isinstance(paypal, dict) or paypal.get("ok"):
        return False
    if str(paypal.get("error_code") or "") == "checkout_unauthorized":
        return True
    error = str(paypal.get("error") or "")
    return "401" in error and "checkout" in error.lower()


def _normalize_payment_method(value):
    value = str(value or "").strip().lower()
    if value in {"gopay", "go-pay", "go_pay"}:
        return "gopay"
    return "paypal"


def _generate_link(access_token, proxy=None, payment_method="paypal"):
    if _normalize_payment_method(payment_method) == "paypal":
        return generate_pp_link(access_token, proxy=proxy)
    return generate_payment_link(access_token, proxy=proxy, payment_method=payment_method)


def _saved_link_matches_payment_method(paypal, payment_method):
    if not isinstance(paypal, dict) or not paypal.get("url"):
        return False
    target = _normalize_payment_method(payment_method)
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


def _load_seed(email="", session_file=""):
    if session_file:
        path = Path(session_file)
        data = _read_json(path)
        return data, str(path)

    record = get_account_record(email) if email else {}
    json_path = str(record.get("json_path") or "").strip()
    data = {}
    if json_path:
        data = _read_json(Path(json_path))
    raw_json = str(record.get("raw_json") or "").strip()
    if raw_json:
        try:
            raw_data = json.loads(raw_json)
            if isinstance(raw_data, dict):
                data = {**raw_data, **data}
        except Exception:
            pass
    if record:
        data.setdefault("email", record.get("email", ""))
        data.setdefault("access_token", record.get("access_token", ""))
        data.setdefault("cookie_header", record.get("cookie_header", ""))
        data.setdefault("oauth_refresh_token", record.get("oauth_refresh_token", ""))
        data.setdefault("refresh_token", record.get("refresh_token", ""))
    return data, json_path


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _access_token(data):
    token = str(data.get("access_token") or "").strip()
    if token:
        return token
    auth_session = data.get("auth_session") if isinstance(data.get("auth_session"), dict) else {}
    for key in ("accessToken", "access_token"):
        value = auth_session.get(key)
        if isinstance(value, str) and value:
            return value
    session = auth_session.get("session") if isinstance(auth_session.get("session"), dict) else {}
    for key in ("accessToken", "access_token"):
        value = session.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
