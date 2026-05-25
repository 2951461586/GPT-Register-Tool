import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from datetime import timezone, datetime

from curl_cffi import requests as curl_requests

from .config import CFG
from .codex_phone import complete_phone_verification
from .codex_sentinel import attach_sentinel, import_cached_auth_cookies, import_cookie_header, load_cached_sentinel, with_sentinel
from .http_client import request_with_retry
from .mailbox import MailboxAccount, _poll_email_otp
from .storage import upsert_account


AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0 Safari/537.36"


def refresh_codex_oauth_session(
    data,
    json_path="",
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
):
    result = collect_codex_oauth_tokens(
        data=data,
        proxy=proxy,
        timeout=timeout,
        force_email_otp_login=force_email_otp_login,
        phone_pool=phone_pool,
    )
    if not result.get("ok"):
        return result
    return _save_oauth_tokens(
        data,
        json_path,
        result["tokens"],
        str(data.get("email") or "").strip().lower(),
        "codex_oauth_pkce",
        result=result,
    )


def collect_codex_oauth_tokens(
    data,
    session=None,
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
):
    email = str(data.get("email") or "").strip().lower()
    if not email:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": "missing_email"}
    oauth = _new_oauth_request()
    if session is None:
        session = curl_requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        import_cookie_header(session, data.get("cookie_header", ""), "chatgpt.com")
        import_cached_auth_cookies(session)
    elif proxy:
        session.proxies = {"http": proxy, "https": proxy}

    try:
        _, current_url = _follow_redirects(session, oauth["auth_url"], proxy=proxy)
        if _has_callback_code(current_url):
            tokens = _exchange_callback(current_url, oauth, proxy=proxy)
            return {"ok": True, "mode": "codex_oauth_pkce", "tokens": tokens}

        result = _login_and_exchange(
            session=session,
            oauth=oauth,
            email=email,
            data=data,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            force_email_otp_login=force_email_otp_login,
            phone_pool=phone_pool,
        )
        if not result.get("ok"):
            return result
        result.setdefault("mode", "codex_oauth_pkce")
        return result
    except Exception as exc:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": str(exc)}


def _login_and_exchange(
    session,
    oauth,
    email,
    data,
    current_url,
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
):
    did = str(data.get("device_id") or "").strip() or _cookie_value(session, "oai-did") or secrets.token_hex(16)
    try:
        session.cookies.set("oai-did", did, domain="auth.openai.com", path="/")
    except Exception:
        pass
    sentinel = load_cached_sentinel()
    headers = _oai_headers(did, {"Referer": current_url or AUTH_URL, "content-type": "application/json"})
    attach_sentinel(headers, sentinel)
    start_resp = session.post(
        "https://auth.openai.com/api/accounts/authorize/continue",
        headers=headers,
        json={"username": {"value": email, "kind": "email"}},
        timeout=30,
        impersonate="chrome110",
        allow_redirects=False,
    )
    if start_resp.status_code != 200:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": f"authorize_continue_failed:{start_resp.status_code}",
            "body": start_resp.text[:300],
        }
    next_url = _next_url(start_resp)
    _, current_url = _follow_redirects(session, next_url, proxy=proxy)
    if _has_callback_code(current_url):
        return {"ok": True, "tokens": _exchange_callback(current_url, oauth, proxy=proxy)}

    return _run_protocol_login_stages(
        session=session,
        oauth=oauth,
        email=email,
        data=data,
        did=did,
        current_url=current_url,
        proxy=proxy,
        timeout=timeout,
        force_email_otp_login=force_email_otp_login,
        phone_pool=phone_pool,
    )


def _run_protocol_login_stages(
    session,
    oauth,
    email,
    data,
    did,
    current_url,
    proxy=None,
    timeout=180,
    force_email_otp_login=False,
    phone_pool=None,
):
    allow_takeover = bool(force_email_otp_login or _allow_passwordless_takeover())
    stage = _detect_protocol_stage(current_url)
    data.setdefault("codex_oauth_protocol", {})
    data["codex_oauth_protocol"]["stage"] = stage
    print(f"[*] Codex OAuth protocol stage: {stage}")

    if _codex_oauth_protocol_ready_stage(stage):
        final = _finish_authorization(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)
        if final.get("ok"):
            final.setdefault("protocol_stage", stage)
        return final

    if stage in {"email", "unknown"}:
        stage = "email_otp" if allow_takeover else stage

    if stage == "email_otp":
        email_otp_result = _passwordless_login_and_exchange(
            session=session,
            oauth=oauth,
            data=data,
            did=did,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            reason="email_otp_required" if _needs_email_otp(current_url) else "email_otp_forced",
            phone_pool=phone_pool,
        )
        if email_otp_result.get("ok"):
            email_otp_result.setdefault("protocol_stage", "email_otp")
            return email_otp_result
    elif stage == "password":
        password_result = _password_login_and_exchange(
            session=session,
            oauth=oauth,
            data=data,
            did=did,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            phone_pool=phone_pool,
        )
        if password_result.get("ok"):
            password_result.setdefault("protocol_stage", "password")
            return password_result
        if not allow_takeover:
            return password_result
        email_otp_result = _passwordless_login_and_exchange(
            session=session,
            oauth=oauth,
            data=data,
            did=did,
            current_url=current_url,
            proxy=proxy,
            timeout=timeout,
            reason="password_login_failed",
            phone_pool=phone_pool,
        )
        if email_otp_result.get("ok"):
            email_otp_result.setdefault("protocol_stage", "email_otp")
            return email_otp_result
        email_otp_result.setdefault("protocol_stage", "email_otp")
        email_otp_result.setdefault("fallback_from", "password_login_failed")
        email_otp_result["password_attempt"] = password_result
        return email_otp_result
    elif stage == "add_phone":
        final = _finish_authorization(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)
        if final.get("ok"):
            final.setdefault("protocol_stage", "add_phone")
            return final
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": (final.get("phone_attempt") or {}).get("error") or "codex_oauth_add_phone_required",
            "last_url": final.get("last_url") or _safe_url(current_url),
            "phone_attempt": final.get("phone_attempt"),
            "protocol_stage": "add_phone",
        }

    email_otp_result = {
        "ok": False,
        "error": f"codex_oauth_protocol_stage_not_ready:{stage}",
        "last_url": _safe_url(current_url),
        "protocol_stage": stage,
    }

    final = _finish_authorization(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)
    if final.get("ok"):
        final.setdefault("protocol_stage", stage)
        return final

    return {
        "ok": False,
        "mode": "codex_oauth_pkce",
        "error": email_otp_result.get("error") or "oauth_callback_code_not_reached",
        "last_url": final.get("last_url") or _safe_url(current_url),
        "email_otp_attempt": email_otp_result,
        "phone_attempt": final.get("phone_attempt"),
        "protocol_stage": stage,
    }


def _passwordless_login_and_exchange(
    session,
    oauth,
    data,
    did,
    current_url,
    proxy=None,
    timeout=180,
    reason="",
    phone_pool=None,
):
    mailbox = _mailbox_from_data(data)
    if mailbox is None:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "passwordless_missing_mailbox",
            "fallback_from": reason,
            "last_url": _safe_url(current_url),
        }

    issued_after = int(time.time()) - 30
    send_result = _send_passwordless_otp(session, did, current_url)
    if send_result.get("hard_error"):
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": send_result.get("error", "passwordless_send_failed"),
            "fallback_from": reason,
            "last_url": _safe_url(current_url),
        }

    attempts = max(1, min(int((CFG.get("email_registration") or {}).get("max_otp_retries") or 3), 5))
    last_error = ""
    last_validate_body = ""
    for attempt in range(attempts):
        if attempt > 0:
            _resend_email_otp(session, did, current_url)
            issued_after = int(time.time()) - 10
        code = _poll_email_otp(
            mailbox,
            subject_keyword=(CFG.get("email_registration") or {}).get("otp_subject_keyword", ""),
            timeout=min(max(int(timeout or 180), 30), 300),
            issued_after_unix=issued_after,
            proxy=proxy,
        )
        if not code:
            last_error = "passwordless_email_otp_poll_timeout"
            continue
        validate = session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=with_sentinel(
                _oai_headers(did, {"Referer": "https://auth.openai.com/email-verification", "content-type": "application/json"}),
                load_cached_sentinel(),
            ),
            json={"code": code},
            timeout=30,
            impersonate="chrome110",
        )
        if validate.status_code != 200:
            last_error = f"email_otp_validate_failed:{validate.status_code}"
            last_validate_body = validate.text[:300]
            print(f"[*] Email OTP validate failed: {validate.status_code} {last_validate_body}")
            if _is_account_deactivated_response(validate.status_code, validate.text):
                return {
                    "ok": False,
                    "mode": "codex_oauth_pkce",
                    "error": "account_deactivated",
                    "terminal": True,
                    "fallback_from": reason,
                    "last_url": _safe_url(current_url),
                    "body": last_validate_body,
                }
            continue
        next_url = _next_url(validate)
        _, current_url = _follow_redirects(session, next_url, proxy=proxy)
        final = _finish_authorization(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)
        if final.get("ok"):
            final["login_method"] = "passwordless_email_otp"
            final["fallback_from"] = reason
            return final
        if final.get("phone_attempt"):
            phone_error = (final.get("phone_attempt") or {}).get("error", "phone_verification_failed")
            return {
                "ok": False,
                "mode": "codex_oauth_pkce",
                "error": phone_error,
                "fallback_from": reason,
                "last_url": final.get("last_url") or _safe_url(current_url),
                "phone_attempt": final.get("phone_attempt"),
            }
        if current_url.endswith("/about-you"):
            return {
                "ok": False,
                "mode": "codex_oauth_pkce",
                "error": "passwordless_about_you_required",
                "fallback_from": reason,
                "last_url": _safe_url(current_url),
            }
    return {
        "ok": False,
        "mode": "codex_oauth_pkce",
        "error": last_error or "passwordless_email_otp_failed",
        "fallback_from": reason,
        "last_url": _safe_url(current_url),
        "body": last_validate_body,
    }


def _password_login_and_exchange(session, oauth, data, did, current_url, proxy=None, timeout=180, phone_pool=None):
    password = str(data.get("password") or "").strip()
    if not password:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": "password_login_required",
            "last_url": _safe_url(current_url),
            "message": "OpenAI routed this account to password login, but the local session data has no account password.",
        }

    response = session.post(
        "https://auth.openai.com/api/accounts/password/verify",
        headers=with_sentinel(
            _oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
            load_cached_sentinel(),
        ),
        json={"password": password},
        timeout=30,
        impersonate="chrome110",
    )
    if response.status_code != 200:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": f"password_verify_failed:{response.status_code}",
            "last_url": _safe_url(current_url),
            "body": response.text[:300],
        }

    _, current_url = _follow_redirects(session, _next_url(response), proxy=proxy)
    if _has_callback_code(current_url):
        return {
            "ok": True,
            "tokens": _exchange_callback(current_url, oauth, proxy=proxy),
            "login_method": "password",
        }

    if _needs_email_otp(current_url):
        email_otp_result = _complete_email_otp(session, data, did, current_url, proxy=proxy, timeout=timeout)
        if not email_otp_result.get("ok"):
            return email_otp_result
        _, current_url = _follow_redirects(session, email_otp_result.get("next_url", ""), proxy=proxy)

    final = _finish_authorization(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)
    if final.get("ok"):
        final["login_method"] = "password"
        return final
    phone_attempt = final.get("phone_attempt") if isinstance(final.get("phone_attempt"), dict) else {}
    if phone_attempt.get("error"):
        final["error"] = phone_attempt.get("error")
    final.setdefault("mode", "codex_oauth_pkce")
    final.setdefault("error", "password_login_oauth_callback_not_reached")
    return final


def _send_passwordless_otp(session, did, current_url):
    sentinel = load_cached_sentinel()
    for endpoint in (
        "https://auth.openai.com/api/accounts/passwordless/send-otp",
        "https://auth.openai.com/api/accounts/email-otp/send",
    ):
        try:
            response = session.post(
                endpoint,
                headers=with_sentinel(
                    _oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
                    sentinel,
                ),
                json={},
                timeout=30,
                impersonate="chrome110",
            )
            if response.status_code == 200:
                print(f"[*] Passwordless OTP send accepted: {endpoint.rsplit('/', 1)[-1]}")
                return {"ok": True, "endpoint": endpoint}
            print(f"[*] Passwordless OTP send skipped: {endpoint.rsplit('/', 1)[-1]} {response.status_code}")
            if response.status_code not in (400, 404, 405):
                return {"ok": False, "hard_error": True, "error": f"passwordless_send_failed:{response.status_code}"}
        except Exception as exc:
            return {"ok": False, "hard_error": True, "error": f"passwordless_send_error:{exc}"}
    return {"ok": False, "error": "passwordless_send_unavailable"}


def _resend_email_otp(session, did, current_url):
    sentinel = load_cached_sentinel()
    try:
        response = session.post(
            "https://auth.openai.com/api/accounts/email-otp/resend",
            headers=with_sentinel(
                _oai_headers(did, {"Referer": "https://auth.openai.com/email-verification", "content-type": "application/json"}),
                sentinel,
            ),
            json={},
            timeout=20,
            impersonate="chrome110",
        )
        print(f"[*] Email OTP resend: {response.status_code}")
    except Exception:
        pass


def _finish_authorization(session, oauth, did, current_url, proxy=None, phone_pool=None):
    if _has_callback_code(current_url):
        return {"ok": True, "tokens": _exchange_callback(current_url, oauth, proxy=proxy)}

    workspace_result = _select_workspace_if_needed(session, did, current_url, proxy=proxy)
    if workspace_result.get("ok"):
        current_url = workspace_result.get("url", current_url)
        if _has_callback_code(current_url):
            return {"ok": True, "tokens": _exchange_callback(current_url, oauth, proxy=proxy)}

    if _needs_phone_verification(current_url):
        if phone_pool:
            print("[*] Waiting for single-phone OAuth lane...")
            with phone_pool.lock:
                return _finish_phone_authorization_locked(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)
        return _finish_phone_authorization_locked(session, oauth, did, current_url, proxy=proxy, phone_pool=phone_pool)

    return {"ok": False, "last_url": _safe_url(current_url)}


def _finish_phone_authorization_locked(session, oauth, did, current_url, proxy=None, phone_pool=None):
    phone_result = complete_phone_verification(
        session,
        did,
        current_url,
        proxy=proxy,
        enabled=bool(phone_pool) or _auto_phone_verification(),
        phone_pool=phone_pool,
    )
    if phone_result.get("ok"):
        current_url = phone_result.get("url") or phone_result.get("next_url") or current_url
        try:
            _, current_url = _follow_redirects(session, current_url, proxy=proxy)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"phone_verified_oauth_redirect_failed:{exc}",
                "last_url": _safe_url(current_url),
                "phone_attempt": phone_result,
            }
        if _has_callback_code(current_url):
            try:
                tokens = _exchange_callback(current_url, oauth, proxy=proxy)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"phone_verified_oauth_token_exchange_failed:{exc}",
                    "last_url": _safe_url(current_url),
                    "phone_attempt": phone_result,
                }
            return {"ok": True, "tokens": tokens, "phone_attempt": phone_result}
        workspace_result = _select_workspace_if_needed(session, did, current_url, proxy=proxy)
        if workspace_result.get("ok"):
            current_url = workspace_result.get("url", current_url)
            if _has_callback_code(current_url):
                try:
                    tokens = _exchange_callback(current_url, oauth, proxy=proxy)
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"phone_verified_oauth_token_exchange_failed:{exc}",
                        "last_url": _safe_url(current_url),
                        "phone_attempt": phone_result,
                    }
                return {"ok": True, "tokens": tokens, "phone_attempt": phone_result}
    return {"ok": False, "last_url": _safe_url(current_url), "phone_attempt": phone_result}


def _complete_email_otp(session, data, did, current_url, proxy=None, timeout=180):
    mailbox = _mailbox_from_data(data)
    if mailbox is None:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": "email_otp_required_missing_mailbox"}
    try:
        session.post(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=_oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
            json={},
            timeout=30,
            impersonate="chrome110",
        )
    except Exception:
        pass
    code = _poll_email_otp(
        mailbox,
        subject_keyword=(CFG.get("email_registration") or {}).get("otp_subject_keyword", ""),
        timeout=min(max(int(timeout or 180), 30), 300),
        issued_after_unix=int(time.time()) - 30,
        proxy=proxy,
    )
    if not code:
        return {"ok": False, "mode": "codex_oauth_pkce", "error": "email_otp_poll_timeout"}
    validate = session.post(
        "https://auth.openai.com/api/accounts/email-otp/validate",
        headers=with_sentinel(
            _oai_headers(did, {"Referer": "https://auth.openai.com/email-verification", "content-type": "application/json"}),
            load_cached_sentinel(),
        ),
        json={"code": code},
        timeout=30,
        impersonate="chrome110",
    )
    if validate.status_code != 200:
        return {
            "ok": False,
            "mode": "codex_oauth_pkce",
            "error": f"email_otp_validate_failed:{validate.status_code}",
            "body": validate.text[:300],
        }
    return {"ok": True, "next_url": _next_url(validate)}


def _select_workspace_if_needed(session, did, current_url, proxy=None):
    if not current_url or not (current_url.endswith("/consent") or current_url.endswith("/workspace")):
        return {"ok": False}
    workspaces = _parse_workspace_from_auth_cookie(_cookie_value(session, "oai-client-auth-session"))
    if not workspaces:
        return {"ok": False}
    workspace_id = ""
    for item in workspaces:
        title = str(item.get("title") or item.get("name") or "")
        if item.get("is_personal") or "Personal" in title:
            workspace_id = str(item.get("id") or "")
            break
    workspace_id = workspace_id or str((workspaces[0] or {}).get("id") or "")
    if not workspace_id:
        return {"ok": False}
    response = session.post(
        "https://auth.openai.com/api/accounts/workspace/select",
        headers=with_sentinel(
            _oai_headers(did, {"Referer": current_url, "content-type": "application/json"}),
            load_cached_sentinel(),
        ),
        json={"workspace_id": workspace_id},
        timeout=30,
        impersonate="chrome110",
    )
    if response.status_code != 200:
        return {"ok": False}
    _, final_url = _follow_redirects(session, _next_url(response), proxy=proxy)
    return {"ok": True, "url": final_url}


def _new_oauth_request():
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return {
        "state": state,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "auth_url": f"{AUTH_URL}?{urllib.parse.urlencode(params)}",
    }


def _exchange_callback(callback_url, oauth, proxy=None):
    parsed = urllib.parse.urlparse(callback_url)
    query = urllib.parse.parse_qs(parsed.query)
    code = (query.get("code") or [""])[0]
    state = (query.get("state") or [""])[0]
    if not code:
        raise RuntimeError("oauth_callback_missing_code")
    if state != oauth["state"]:
        raise RuntimeError("oauth_state_mismatch")
    response = request_with_retry(
        curl_requests,
        "post",
        TOKEN_URL,
        label="OAuth token exchange",
        attempts=5,
        retry_delay=5,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": oauth["redirect_uri"],
            "code_verifier": oauth["code_verifier"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        proxies={"http": proxy, "https": proxy} if proxy else None,
        impersonate="chrome110",
    )
    if response.status_code != 200:
        raise RuntimeError(f"oauth_token_exchange_failed:{response.status_code}:{response.text[:300]}")
    body = response.json()
    if not body.get("access_token") or not body.get("refresh_token"):
        raise RuntimeError("oauth_token_response_missing_access_or_refresh_token")
    return body


def _save_oauth_tokens(data, json_path, tokens, email, mode, result=None):
    now = int(time.time())
    expires_in = _as_int(tokens.get("expires_in")) or 0
    refreshed = dict(data)
    refreshed["email"] = email
    refreshed["success"] = True
    refreshed["access_token"] = tokens.get("access_token", "")
    refreshed["id_token"] = tokens.get("id_token", "")
    refreshed["oauth_refresh_token"] = tokens.get("refresh_token", "")
    refreshed["refresh_token_status"] = "oauth_present"
    refreshed["refresh_token_updated_at"] = now
    refreshed["refreshed_at"] = now
    refreshed.pop("error", None)
    if str(refreshed.get("status") or "").lower() == "failed":
        refreshed.pop("status", None)
    refreshed["codex_oauth"] = {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "mode": mode,
        "updated_at": now,
    }
    result = result if isinstance(result, dict) else {}
    phone_attempt = result.get("phone_attempt") if isinstance(result.get("phone_attempt"), dict) else {}
    if phone_attempt:
        refreshed["phone"] = phone_attempt.get("phone", refreshed.get("phone", ""))
        response = refreshed.get("response") if isinstance(refreshed.get("response"), dict) else {}
        response["phone_verification"] = phone_attempt
        response["codex_oauth"] = {
            "ok": True,
            "mode": mode,
            "has_access_token": bool(tokens.get("access_token")),
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "phone_verified": bool(phone_attempt.get("ok")),
        }
        refreshed["response"] = response
    if expires_in:
        refreshed["oauth_expires_at"] = _iso_utc(now + expires_in)
    if json_path:
        from pathlib import Path
        Path(json_path).write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
    upsert_account(refreshed, json_path=json_path)
    return {
        "ok": True,
        "mode": mode,
        "email": email,
        "json_path": json_path,
        "refresh_token_status": "oauth_present",
    }


def _follow_redirects(session, start_url, proxy=None, max_redirects=18):
    current_url = _absolute_url("https://auth.openai.com", start_url)
    response = None
    for _ in range(max_redirects):
        if not current_url:
            return response, current_url
        response = request_with_retry(
            session,
            "get",
            current_url,
            label="OAuth redirect",
            attempts=5,
            retry_delay=5,
            allow_redirects=False,
            impersonate="chrome110",
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            return response, current_url
        location = response.headers.get("Location", "")
        if not location:
            return response, current_url
        current_url = urllib.parse.urljoin(current_url, location)
        if _has_callback_code(current_url):
            return response, current_url
    return response, current_url


def _mailbox_from_data(data):
    mailbox = data.get("mailbox") if isinstance(data.get("mailbox"), dict) else {}
    email = str(mailbox.get("email") or data.get("email") or "").strip()
    refresh_token = str(mailbox.get("refresh_token") or "").strip()
    provider = str(mailbox.get("provider") or "").strip()
    if not email:
        return None
    if provider != "cfworker" and not refresh_token:
        return None
    return MailboxAccount(
        email=email,
        password=str(mailbox.get("password") or data.get("password") or "").strip(),
        refresh_token=refresh_token,
        access_token=str(mailbox.get("access_token") or "").strip(),
        token=str(mailbox.get("token") or "").strip(),
        source=str(mailbox.get("source") or "").strip(),
        provider=provider,
    )


def _oai_headers(did, extra=None):
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": USER_AGENT,
        "sec-ch-ua": '"Google Chrome";v="110", "Chromium";v="110", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "oai-device-id": did,
    }
    if extra:
        headers.update(extra)
    return headers


def _next_url(response):
    try:
        body = response.json()
    except Exception:
        body = {}
    return _absolute_url("https://auth.openai.com", body.get("continue_url") or response.headers.get("Location") or response.url)


def _needs_email_otp(url):
    value = str(url or "").lower()
    return "email-verification" in value or "email-otp" in value


def _needs_password(url):
    value = str(url or "").lower()
    return "/log-in/password" in value or "/login/password" in value or value.endswith("/password")


def _needs_phone_verification(url):
    value = str(url or "").lower()
    return "/add-phone" in value or "phone-verification" in value


def _detect_protocol_stage(url):
    value = str(url or "")
    lower = value.lower()
    if _has_callback_code(value):
        return "callback"
    if _needs_phone_verification(lower):
        return "add_phone"
    if _needs_email_otp(lower):
        return "email_otp"
    if _needs_password(lower):
        return "password"
    if lower.endswith("/consent") or lower.endswith("/workspace"):
        return "consent"
    if "/authorize" in lower or "/oauth/" in lower:
        return "email"
    return "unknown"


def _codex_oauth_protocol_ready_stage(stage):
    return stage in {"callback", "consent"}


def _codex_oauth_cfg():
    return CFG.get("codex_oauth") if isinstance(CFG.get("codex_oauth"), dict) else {}


def _allow_passwordless_takeover():
    return bool(_codex_oauth_cfg().get("allow_passwordless_takeover", False))


def _auto_phone_verification():
    return bool(_codex_oauth_cfg().get("auto_phone_verification", False))


def _is_account_deactivated_response(status_code, text):
    body = str(text or "").lower()
    return int(status_code or 0) in (403, 404) and (
        "account_deactivated" in body
        or "deleted or deactivated" in body
        or "account has been deleted" in body
        or "account has been deactivated" in body
    )


def _has_callback_code(url):
    text = str(url or "")
    return "code=" in text and "state=" in text


def _cookie_value(session, name):
    try:
        return session.cookies.get(name) or ""
    except Exception:
        return ""


def _parse_workspace_from_auth_cookie(auth_cookie):
    if not auth_cookie or "." not in auth_cookie:
        return []
    parts = auth_cookie.split(".")
    for segment in parts[1:2] + parts[:1]:
        claims = _jwt_segment(segment)
        workspaces = claims.get("workspaces") or []
        if isinstance(workspaces, list) and workspaces:
            return workspaces
    return []


def _jwt_segment(segment):
    try:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _absolute_url(base_url, url):
    if not url:
        return ""
    if str(url).startswith(("http://", "https://")):
        return str(url)
    return base_url.rstrip("/") + "/" + str(url).lstrip("/")


def _safe_url(url):
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return str(url or "")[:200]


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return 0


def _iso_utc(epoch_seconds):
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
