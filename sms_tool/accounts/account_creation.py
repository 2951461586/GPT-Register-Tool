import json
import logging
import time

from ..codex_sentinel import load_cached_sentinel, with_sentinel
from ..auth_headers import auth_impersonate, openai_auth_headers
from ..config import CFG
from ..http_client import request_with_retry
from ..http_utils import (
    _absolute_url,
    _cookie_header,
    _cookie_presence,
    _json_or_raw,
    _minimal_chatgpt_cookie_header,
)

_LOGGER = logging.getLogger(__name__)

def _create_account_sentinel_token(sentinel_data, proxy=None):
    token = str((sentinel_data or {}).get("sentinel_oauth_token") or "").strip()
    if token:
        return token
    raise RuntimeError("sentinel_extract_failed: oauth_create_account SDK token is required")




def _email_otp_send_url(reg_data, auth_base, resume_email_verification=False):
    continue_url = ""
    if isinstance(reg_data, dict):
        continue_url = str(reg_data.get("continue_url") or "").strip()
    if continue_url:
        return continue_url
    if resume_email_verification:
        return _absolute_url(auth_base, "/api/accounts/email-otp/send")
    return ""


def _create_account_continue_url(create_data):
    if not isinstance(create_data, dict):
        return ""
    continue_url = str(create_data.get("continue_url") or "").strip()
    if continue_url:
        return continue_url
    error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
    return str(error.get("redirect_uri") or error.get("redirect_url") or "").strip()


def _is_user_already_exists(create_data):
    if not isinstance(create_data, dict):
        return False
    error = create_data.get("error") if isinstance(create_data.get("error"), dict) else {}
    return str(error.get("code") or "").strip() == "user_already_exists"

def _validate_email_otp(session, auth_base, base_headers, code, sentinel_data=None, use_sentinel=True):
    # Primary endpoint: same as codex_oauth (proven working)
    primary_endpoint = "/api/accounts/email-otp/validate"
    fallback_endpoints = [
        "/api/accounts/email-verification/validate",
        "/api/accounts/email-verification/verify",
        "/api/accounts/verify-email",
    ]
    did = str((base_headers or {}).get("oai-device-id") or (base_headers or {}).get("Oai-Device-Id") or "").strip()
    validate_headers = {
        **(base_headers or {}),
        **openai_auth_headers(
            did,
            referer=f"{auth_base}/email-verification",
            origin=auth_base,
            extra={"content-type": "application/json"},
        ),
    }
    if use_sentinel:
        sentinel = sentinel_data or load_cached_sentinel()
        validate_headers = with_sentinel(validate_headers, sentinel)
    # Try primary endpoint first with {"code": payload (matches codex_oauth)
    url = _absolute_url(auth_base, primary_endpoint)
    r = request_with_retry(session, "post", url, label=f"Email OTP validate {primary_endpoint}",
        json={"code": code}, headers=validate_headers, impersonate=auth_impersonate())
    body = _json_or_raw(r)
    if r.status_code == 200:
        print(f"  Email OTP validate: {primary_endpoint} {r.status_code}")
        return True, body
    last_error = {"endpoint": primary_endpoint, "status": r.status_code, "body": body}
    print(f"  Email OTP validate: {primary_endpoint} {r.status_code} {json.dumps(body, ensure_ascii=False, default=str)[:200]}")
    # If primary returns 404/405, try fallback endpoints
    if r.status_code in (404, 405):
        for endpoint in fallback_endpoints:
            url = _absolute_url(auth_base, endpoint)
            for payload in ({"code": code}, {"otp": code}):
                r = request_with_retry(session, "post", url, label=f"Email OTP validate {endpoint}",
                    json=payload, headers=validate_headers, impersonate=auth_impersonate())
                body = _json_or_raw(r)
                if r.status_code == 200:
                    print(f"  Email OTP validate: {endpoint} {r.status_code}")
                    return True, body
                if r.status_code not in (404, 405):
                    last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
                    print(f"  Email OTP validate failed: {endpoint} {r.status_code} {json.dumps(body, ensure_ascii=False, default=str)[:200]}")
                    break
                last_error = {"endpoint": endpoint, "status": r.status_code, "body": body}
    return False, last_error


def _is_wrong_email_otp_code(data):
    try:
        error = (data or {}).get("body", {}).get("error", {})
        code = str(error.get("code") or "").strip().lower()
        message = str(error.get("message") or "").strip().lower()
        return code == "wrong_email_otp_code" or "wrong code" in message
    except Exception:
        return False




def _extract_nested(data, *keys):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current or ""


def _auth_session_access_token(body):
    return (
        body.get("accessToken")
        or body.get("access_token")
        or _extract_nested(body, "session", "access_token")
        or _extract_nested(body, "session", "accessToken")
    )


def _session_body_shape(body) -> str:
    """Credential-free description of a session response body.

    ``_json_or_raw`` returns ``{"_raw": ...}`` when the body is not JSON, and
    the parsed object otherwise -- so the *key set* is what separates the two
    failure modes that a bare ``Auth session: 200`` line cannot: a non-JSON
    interstitial versus a JSON session that simply has not propagated yet.
    Key **names** only; values are never included.
    """
    if not isinstance(body, dict):
        return f"type={type(body).__name__}"
    if "_raw" in body:
        return f"non_json raw_len={len(str(body.get('_raw') or ''))}"
    keys = sorted(str(key) for key in body.keys())
    return f"json keys={keys[:8]}"


def _contains_access_token_key(node, depth: int = 0) -> bool:
    """True when a **non-empty** ``accessToken`` / ``access_token`` value exists.

    Distinguishes "the session really has no token" from "the token is present
    but parked on a path ``_auth_session_access_token`` does not walk" -- the
    latter is an extractor bug and would otherwise be indistinguishable.

    🔴 The value must be non-empty.  An anonymous ChatGPT session can answer
    ``{"accessToken": null, "user": null, ...}``: the *key* is there, so a
    key-presence-only probe reports ``token_key_present=True`` and sends the
    reader chasing an extractor bug that does not exist.  ``_auth_session_access_token``
    uses ``or`` chaining, so a null/empty value is not a token to it either --
    the probe must agree with the extractor, not merely with the key set.
    """
    if depth > 4:
        return False
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key) in {"accessToken", "access_token"}:
                if value:
                    return True
                continue
            if _contains_access_token_key(value, depth + 1):
                return True
    elif isinstance(node, list):
        return any(_contains_access_token_key(item, depth + 1) for item in node)
    return False


def _fetch_auth_session(session, chat_base, base_headers, attempts=4, delay=0.5):
    """Fetch the post-signup session with a short, bounded readiness poll.

    The old six-round poll used the global HTTP retry policy on every round,
    which multiplied a single slow edge response into multi-minute waits.  A
    session endpoint is cheap to retry, so each round is now one request with
    a small backoff and explicit timing metadata for diagnostics.
    """
    started = time.monotonic()
    try:
        request_timeout = max(5, min(int((CFG.get("timeouts") or {}).get("auth_session", 12)), 30))
    except (TypeError, ValueError):
        request_timeout = 12
    last = {"status_code": 0, "body": {}, "cookie_header": _cookie_header(session)}
    for attempt in range(1, max(1, int(attempts or 1)) + 1):
        r = request_with_retry(session, "get", f"{chat_base}/api/auth/session", label="Auth session",
            headers={**base_headers, "Accept": "application/json", "Origin": chat_base, "Referer": f"{chat_base}/"},
            impersonate=auth_impersonate(), attempts=1, timeout=request_timeout)
        body = _json_or_raw(r, limit=1000)
        last = {
            "status_code": r.status_code,
            "body": body,
            "cookie_header": _cookie_header(session),
            "attempts": attempt,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        at_present = bool(_auth_session_access_token(body))
        print(f"  Auth session: {r.status_code}" + (f" attempt={attempt}" if attempt > 1 else ""))
        # `print` never reaches sms_tool.log in production (the file handlers
        # only receive logging records), so a run that exhausted the readiness
        # poll left no trace of *what* came back.  2026-09-12: four consecutive
        # HTTP 200s carrying no access token, and no way to tell a Cloudflare
        # interstitial from a JSON session that had not propagated -- or from a
        # token sitting on a path the extractor does not walk.
        #
        # 🔴 Field names are chosen to survive `sanitize_log_text`: the policy's
        # `named_secret` rule matches ``<something>token=value`` and rewrites it
        # to ``[REDACTED]``, which would make True and False indistinguishable in
        # the one line an operator greps.  ``access_token_present`` ends in the
        # policy's `safe_key_suffixes` entry ``_present``; ``has_access_token``
        # was the original name and both its message text and its `.jsonl` field
        # came out as ``[REDACTED]`` (measured, not assumed).
        token_key_present = _contains_access_token_key(body)
        shape = _session_body_shape(body)
        jar = _cookie_presence(session)
        _LOGGER.info(
            "Auth session readiness status=%s attempt=%s/%s access_token_present=%s "
            "token_key_present=%s nextauth_session=%s cookie_count=%s shape=%s",
            r.status_code,
            attempt,
            attempts,
            at_present,
            token_key_present,
            jar.get("nextauth_session"),
            jar.get("cookie_count"),
            shape,
            extra={
                "event": "auth_session_readiness",
                "status_code": r.status_code,
                "attempt": attempt,
                "max_attempts": attempts,
                "access_token_present": at_present,
                "token_key_present": token_key_present,
                "body_shape": shape,
                # Flat copies so `.jsonl` consumers can grep one field without
                # walking the nested dict.
                "nextauth_session": jar.get("nextauth_session"),
                "cookie_count": jar.get("cookie_count"),
                # `jar_presence`, not `cookie_presence`: the policy's
                # `sensitive_key_fragments` contains "cookie" and redacts the
                # whole dict (measured).  "jar" is unambiguous in context.
                "jar_presence": jar,
            },
        )
        if r.status_code == 200 and at_present:
            return last
        if attempt < attempts:
            time.sleep(min(max(0.0, float(delay or 0)), 2.0))
    return last
# ==========================================
# Core Email Registration Flow
# ==========================================
